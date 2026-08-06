# -*- coding: utf-8 -*-
"""手动集成验证：在隔离端口/目录下真实拉起 4 个服务并检查三级健康端点。

不放进 pytest 自动跑——它会真的启动 4 个 uvicorn 进程、写数据目录，耗时
十几秒，且与用户已安装的桌面版抢资源。改动 launcher / 健康端点 / 服务启动
逻辑后手动执行一次即可：

    .venv/Scripts/python.exe tests/manual_launch_check.py

端口用 895x 段（而非正式的 891x），避免与已安装的桌面版冲突。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_PORTS = {"feishu_agent": 8951, "ai_router": 8952,
              "agent_pipeline": 8953, "spider": 8954}


def http_json(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gmd_launch_check_"))
    print(f"[setup] 隔离运行目录: {tmp}")

    from common import config as cfg_mod
    cfg_mod.APP_DIR = tmp
    cfg_mod.CONFIG_PATH = tmp / "config" / "config.json"
    cfg = cfg_mod.load_config()
    cfg["ports"] = dict(TEST_PORTS)
    cfg_mod.save_config(cfg)

    sys.modules.pop("launcher", None)
    import launcher

    failed = 0
    try:
        print("[start] 按拓扑顺序启动:",
              " -> ".join(s["name"] for s in launcher.SERVICES))
        launcher.start_all()

        # 给服务留出加载时间（ai-router 要初始化 Chroma）
        deadline = time.time() + 60
        while time.time() < deadline:
            if all(http_json(f"http://127.0.0.1:{p}/api/health/live")[0] == 200
                   for p in TEST_PORTS.values()):
                break
            time.sleep(2)

        print("\n=== liveness ===")
        for name, port in TEST_PORTS.items():
            code, body = http_json(f"http://127.0.0.1:{port}/api/health/live")
            ok = code == 200 and body.get("status") == "alive"
            print(f"  {'PASS' if ok else 'FAIL'}  {name:<16} {code} {body}")
            failed += 0 if ok else 1

        print("\n=== readiness ===")
        for name, port in TEST_PORTS.items():
            code, body = http_json(f"http://127.0.0.1:{port}/api/health/ready",
                                   timeout=15)
            status = body.get("status", "?")
            # healthy / degraded 都算通过（无 AI Key、未登录闲鱼是预期降级）
            ok = code in (200, 503) and status in ("healthy", "degraded")
            print(f"  {'PASS' if ok else 'FAIL'}  {name:<16} HTTP {code} "
                  f"status={status}")
            for reason in body.get("reasons", []):
                print(f"          - {reason}")
            failed += 0 if ok else 1

        print("\n=== 日志脱敏抽查 ===")
        logdir = tmp / "data" / "logs"
        leaked = []
        for lf in sorted(logdir.glob("*.log")):
            text = lf.read_text(encoding="utf-8", errors="ignore")
            for marker in ("sk-", "cli_", "AIza"):
                for line in text.splitlines():
                    if marker in line and "REDACTED" not in line:
                        leaked.append(f"{lf.name}: {line.strip()[:120]}")
        if leaked:
            failed += 1
            print("  FAIL  日志中发现疑似未脱敏凭据:")
            for x in leaked[:10]:
                print(f"        {x}")
        else:
            print(f"  PASS  {len(list(logdir.glob('*.log')))} 个日志文件未见明文凭据")

    finally:
        print("\n[teardown] 停止服务...")
        try:
            launcher.stop_all()
        except Exception as e:
            print(f"  停止异常: {e}")
        time.sleep(2)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'全部通过' if failed == 0 else f'{failed} 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
