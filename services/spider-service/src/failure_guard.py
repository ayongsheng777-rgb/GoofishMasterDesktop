"""Task-level failure circuit breaker.

目标:
- 当登录态失效/风控导致任务持续失败时，避免无限重试、避免高频请求。
- 失败达到阈值后暂停任务一段时间。
- 暂停期间最多每天通知一次，直到用户更新 cookies / 登录态文件后自动恢复。

说明:
- 仅使用标准库，既可被 API 主进程使用，也可被爬虫子进程使用。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


from datetime import timezone

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


# Windows / 精简打包(PyInstaller)环境下无系统 IANA 时区库且未安装 tzdata 时，
# ZoneInfo("Asia/Shanghai") 会在【调用时】抛 ZoneInfoNotFoundError:
# "No time zone found with key Asia/Shanghai"（导入 zoneinfo 本身并不报错）。
# Asia/Shanghai 自 1991 年起无夏令时，固定 UTC+8 与之完全等价，作为兜底。
_FIXED_CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _load_tz(name: str):
    """加载时区；数据缺失时回退到固定东八区(仅 Asia/Shanghai)，永不抛异常。"""
    if ZoneInfo is None:
        return _FIXED_CN_TZ if name == "Asia/Shanghai" else None
    try:
        return ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError 等：tzdata 未安装/未打包
        return _FIXED_CN_TZ if name == "Asia/Shanghai" else None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now(tz_name: str, now: Optional[datetime] = None) -> datetime:
    if now is not None:
        return now
    tz = _load_tz(tz_name)
    if tz is None:
        return datetime.now()
    return datetime.now(tz)


def _today_str(tz_name: str, now: Optional[datetime] = None) -> str:
    return _now(tz_name, now=now).date().isoformat()


def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _str_to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _get_mtime(path: Optional[str]) -> Optional[float]:
    if not path:
        return None
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _cookie_changed(
    cookie_path: Optional[str], previous_mtime: Optional[float]
) -> bool:
    if not cookie_path:
        return False
    current = _get_mtime(cookie_path)
    if current is None or previous_mtime is None:
        return False
    return current > (previous_mtime + 1e-6)


class _FileLock:
    """跨进程文件锁：Unix 用 fcntl.flock；Windows 用 msvcrt.locking。

    注意：Windows 下锁的是文件字节区间，且【锁随句柄关闭而释放】——因此
    _update_task 在持有锁句柄期间绝不能 os.replace 该文件（WinError 5），
    参见 _update_task 的读写分离实现。
    """

    def __init__(self, fh):
        self._fh = fh

    def __enter__(self):
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        return False


def _default_state_path() -> str:
    """默认状态文件路径：DATA_DIR 感知，兜底落在项目内（与 launcher 注入一致）。

    旧版默认 "logs/task-failure-guard.json" 是相对【当前工作目录】的——桌面端
    CWD 不可控（安装到 Program Files 时不可写），2026-08-05 改为绝对路径。
    """
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        return str(Path(data_dir) / "logs" / "task-failure-guard.json")
    # 与 spider-service/main.py 的 DATA_DIR 兜底保持一致：项目根/data/spider
    return str(Path(__file__).resolve().parents[3] / "data" / "spider" / "logs" / "task-failure-guard.json")


# 进程内写串行锁。旧版用 fcntl.flock 做跨进程锁，但 Windows 无 fcntl 模块
# （ImportError 被静默吞掉 => 实际无锁），故仅保证进程内串行；跨进程并发写
# 由「唯一 tmp 名 + os.replace 原子性 + 重试」容忍（最坏丢一条计数，可接受）。
_PROCESS_LOCK = threading.Lock()


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _read_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 文件损坏时保留现场，避免无限解析失败。
        try:
            ts = str(int(time.time()))
            os.replace(path, f"{path}.corrupt.{ts}")
        except Exception:
            pass
        return {}


def _atomic_write_json(path: str, data: dict) -> None:
    _ensure_parent_dir(path)
    # tmp 名唯一化：避免多进程/多线程并发写同一 tmp 互相截断。
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{int(time.time() * 1000)}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    # Windows 上杀软/搜索索引可能短暂占用目标文件导致 WinError 5，退避重试。
    last_err: Optional[OSError] = None
    for delay in (0, 0.1, 0.3, 1.0):
        if delay:
            time.sleep(delay)
        try:
            os.replace(tmp, path)
            return
        except OSError as e:
            last_err = e
    # 最终失败：尽力清理 tmp 后抛出（由调用方决定是否兜底）。
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise last_err  # type: ignore[misc]


@dataclass(frozen=True)
class SkipDecision:
    skip: bool
    should_notify: bool
    reason: str
    paused_until: Optional[datetime]
    consecutive_failures: int


class FailureGuard:
    def __init__(
        self,
        path: Optional[str] = None,
        *,
        threshold: Optional[int] = None,
        pause_seconds: Optional[int] = None,
        tz_name: Optional[str] = None,
    ):
        self.path = (
            path
            or os.getenv("TASK_FAILURE_GUARD_PATH")
            or _default_state_path()
        )
        self.threshold = max(
            1, threshold or _as_int(os.getenv("TASK_FAILURE_THRESHOLD"), 3)
        )
        self.pause_seconds = max(
            60,
            pause_seconds
            or _as_int(os.getenv("TASK_FAILURE_PAUSE_SECONDS"), 24 * 60 * 60),
        )
        self.tz_name = tz_name or os.getenv("TASK_FAILURE_TZ") or "Asia/Shanghai"

    def _load(self) -> dict:
        data = _read_json_file(self.path)
        if "tasks" not in data or not isinstance(data.get("tasks"), dict):
            data = {"version": 1, "tasks": {}}
        data.setdefault("version", 1)
        return data

    def _save(self, data: dict) -> None:
        _atomic_write_json(self.path, data)

    def _update_task(self, task_key: str, updater) -> dict:
        """读-改-写一个任务条目。

        Windows 修复（2026-08-05，WinError 5）：旧实现以 `open(self.path, "a+")`
        持有目标文件句柄，在句柄未关闭时调用 `_save` → `os.replace(tmp, path)`，
        Windows 不允许替换被打开（无 FILE_SHARE_DELETE）的文件 → WinError 5 拒绝访问。
        现改为读写分离：锁内仅读取并随即关闭句柄，写盘时不再持有目标文件。
        跨进程读改写存在窄竞态窗口（读后再写），由 replace 原子性兜底，最坏丢失
        一条并发计数——对本「尽力记录」场景可接受。
        """
        _ensure_parent_dir(self.path)
        with _PROCESS_LOCK:
            # 锁内读取（文件可能正被其他进程原子替换，msvcrt 锁降低读到半截的概率）
            try:
                with open(self.path, "a+", encoding="utf-8") as fh:
                    with _FileLock(fh):
                        fh.seek(0)
                        try:
                            data = json.load(fh)
                            if not isinstance(data, dict):
                                data = {}
                        except Exception:
                            data = self._load()
            except FileNotFoundError:
                data = {"version": 1, "tasks": {}}
            # 句柄已关闭 —— 以下写盘不再持有目标文件
            if "tasks" not in data or not isinstance(data.get("tasks"), dict):
                data = {"version": 1, "tasks": {}}
            data.setdefault("version", 1)
            tasks = data["tasks"]
            entry = tasks.get(task_key) or {}
            if not isinstance(entry, dict):
                entry = {}
            entry = updater(entry) or entry
            tasks[task_key] = entry
            self._save(data)
            return entry

    def record_success(self, task_key: str, *, now: Optional[datetime] = None) -> None:
        def _reset(_: dict) -> dict:
            current = _now(self.tz_name, now=now)
            return {
                "consecutive_failures": 0,
                "paused_until": None,
                "last_notified_date": None,
                "last_failure_reason": None,
                "last_failure_at": None,
                "last_success_at": _dt_to_str(current),
                "cookie_path": None,
                "cookie_mtime": None,
            }

        try:
            self._update_task(task_key, _reset)
        except Exception as e:
            # 记账是辅助能力，写盘失败绝不能搞挂采集主流程（2026-08-05 教训：
            # WinError 5 从本模块冒泡，把真实采集失败原因掩盖成 guard 自身错误）
            print(f"[FailureGuard] record_success 状态写入失败（已忽略）: {e}")

    def should_skip_start(
        self,
        task_key: str,
        *,
        cookie_path: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> SkipDecision:
        current = _now(self.tz_name, now=now)
        today = _today_str(self.tz_name, now=current)

        data = self._load()
        entry = (data.get("tasks") or {}).get(task_key) or {}
        if not isinstance(entry, dict):
            entry = {}

        paused_until = _str_to_dt(entry.get("paused_until"))
        consecutive = _as_int(entry.get("consecutive_failures"), 0)
        last_reason = (entry.get("last_failure_reason") or "").strip() or "未知错误"
        last_notified_date = entry.get("last_notified_date")

        previous_cookie_mtime = entry.get("cookie_mtime")
        if cookie_path and previous_cookie_mtime is not None:
            try:
                previous_cookie_mtime = float(previous_cookie_mtime)
            except (TypeError, ValueError):
                previous_cookie_mtime = None

        if (
            paused_until
            and paused_until > current
            and cookie_path
            and _cookie_changed(cookie_path, previous_cookie_mtime)
        ):
            # cookies / 登录态更新 => 自动恢复
            self.record_success(task_key, now=current)
            return SkipDecision(
                skip=False,
                should_notify=False,
                reason="cookie_updated",
                paused_until=None,
                consecutive_failures=0,
            )

        if paused_until and current < paused_until:
            should_notify = last_notified_date != today

            if should_notify:

                def _touch(e: dict) -> dict:
                    e = dict(e or {})
                    e["last_notified_date"] = today
                    return e

                try:
                    self._update_task(task_key, _touch)
                except Exception as e:
                    print(f"[FailureGuard] 通知标记写入失败（已忽略）: {e}")

            return SkipDecision(
                skip=True,
                should_notify=should_notify,
                reason=last_reason,
                paused_until=paused_until,
                consecutive_failures=consecutive,
            )

        return SkipDecision(
            skip=False,
            should_notify=False,
            reason="not_paused",
            paused_until=None,
            consecutive_failures=consecutive,
        )

    def record_failure(
        self,
        task_key: str,
        reason: str,
        *,
        cookie_path: Optional[str] = None,
        min_failures_to_pause: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> dict:
        current = _now(self.tz_name, now=now)
        today = _today_str(self.tz_name, now=current)
        cookie_mtime = _get_mtime(cookie_path)

        effective_threshold = max(1, int(min_failures_to_pause or self.threshold))

        result = {
            "should_notify": False,
            "opened_circuit": False,
            "paused_until": None,
            "consecutive_failures": 0,
        }

        def _apply(entry: dict) -> dict:
            entry = dict(entry or {})
            previous_paused_until = _str_to_dt(entry.get("paused_until"))
            was_paused = bool(previous_paused_until and current < previous_paused_until)

            prev_mtime = entry.get("cookie_mtime")
            try:
                prev_mtime = float(prev_mtime) if prev_mtime is not None else None
            except (TypeError, ValueError):
                prev_mtime = None

            if cookie_path and _cookie_changed(cookie_path, prev_mtime):
                entry["consecutive_failures"] = 0
                entry["paused_until"] = None
                entry["last_notified_date"] = None

            consecutive = _as_int(entry.get("consecutive_failures"), 0) + 1
            entry["consecutive_failures"] = consecutive
            entry["last_failure_reason"] = (reason or "未知错误")[:1000]
            entry["last_failure_at"] = _dt_to_str(current)
            if cookie_path:
                entry["cookie_path"] = cookie_path
                if cookie_mtime is not None:
                    entry["cookie_mtime"] = cookie_mtime

            opened = False
            if consecutive >= effective_threshold:
                paused_until = current + timedelta(seconds=self.pause_seconds)
                entry["paused_until"] = _dt_to_str(paused_until)
                opened = not was_paused

                if entry.get("last_notified_date") != today:
                    entry["last_notified_date"] = today
                    result["should_notify"] = True

                result["paused_until"] = paused_until
            else:
                entry["paused_until"] = None

            result["opened_circuit"] = opened
            result["consecutive_failures"] = consecutive
            return entry

        try:
            self._update_task(task_key, _apply)
        except Exception as e:
            # 记账失败绝不搞挂主流程；保守放开通知，避免真实失败被静默
            print(f"[FailureGuard] record_failure 状态写入失败（已忽略）: {e}")
            result["should_notify"] = True
        return result
