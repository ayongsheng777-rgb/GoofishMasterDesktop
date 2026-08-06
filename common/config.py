# -*- coding: utf-8 -*-
"""
GoofishMasterDesktop 配置中心
读取 config/config.json，提供端口映射、后端地址、AI Key 等。
与原 Docker 项目完全解耦：端口整体偏移 +10（8911-8914），后端地址指向 127.0.0.1。
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from common import secretstore
except ImportError:  # 服务子进程可能以 services/<svc> 为 cwd 直接导入本模块
    import secretstore  # type: ignore

# 资源根目录（只读）：services / knowledge-base / common 模块所在地。
# 冻结感知：PyInstaller onedir 下资源位于 sys._MEIPASS（_internal 目录）。
if getattr(sys, "frozen", False):
    _mp = getattr(sys, "_MEIPASS", None)
    ROOT = Path(_mp).resolve() if _mp else Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent

# 可写应用目录：config.json、运行日志、各服务数据落在此处。
# 优先 exe 同目录（便携版可直接跑）；若不可写（如安装到 Program Files）
# 则回退到 %APPDATA%/GoofishMasterDesktop，保证安装版也能正常运行。
def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates = [exe_dir]
        _ad = os.environ.get("APPDATA")
        if _ad:
            # 向后兼容：旧版本落在 %APPDATA%/goofish-server，存在则继续沿用
            _legacy = Path(_ad) / "goofish-server"
            candidates.append(_legacy if _legacy.is_dir()
                              else Path(_ad) / "GoofishMasterDesktop")
        _legacy_home = Path.home() / ".goofish-server"
        candidates.append(_legacy_home if _legacy_home.is_dir()
                          else Path.home() / ".GoofishMasterDesktop")
    else:
        candidates = [ROOT]
    for _c in candidates:
        try:
            _c.mkdir(parents=True, exist_ok=True)
            _t = _c / ".wtest"
            _t.write_text("1", encoding="utf-8")
            _t.unlink()
            return _c
        except Exception:
            continue
    return candidates[0]

APP_DIR = _app_dir()
CONFIG_PATH = APP_DIR / "config" / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"

# 当前版本号（check_update 用）。**发版时务必与 GoofishMasterDesktop.iss 的
# MyAppVersion 同步**——安装器不会把 .iss 打进包里，运行期只能读这里。
APP_VERSION = "1.1.4"

# 版本检查目标仓库（releases/latest）。若仓库将来转 Private，匿名 API 会
# 404，check_update 静默判为"检查失败"，不影响控制台任何功能。
GITHUB_REPO = "ayongsheng777-rgb/GoofishMasterDesktop"

# 服务名 → 默认端口（与原 Docker 8901-8904 偏移 +10，避免与本机运行的原项目冲突）
DEFAULT_PORTS = {
    "feishu_agent": 8911,
    "ai_router": 8912,
    "agent_pipeline": 8913,
    "spider": 8914,
}

# P1 单用户化：三个可选数据库已全部改为进程内嵌入式实现，默认全开：
#   postgres -> SQLite（aiosqlite，DATA_DIR/goofish.db）
#   redis    -> fakeredis（进程内、API 兼容）
#   qdrant   -> Chroma（chromadb.PersistentClient，DATA_DIR/chroma）
# 零外部依赖、随 exe 同级落盘；enabled=false 仍可优雅降级（功能禁用）。
DEFAULT_BACKENDS = {
    "postgres": {"enabled": True, "port": 5439, "user": "goofish",
                 "password": "goofish_v2_secret", "db": "goofish_ai"},
    "redis": {"enabled": True, "port": 6399},
    "qdrant": {"enabled": True, "port": 6339},
}


# 存储引擎：桌面版固定 sqlite（进程内嵌入式）。保留字段是为了让
# 「配置声明的存储」与「代码实际使用的存储」一致——此前 backend_urls()
# 生成 postgresql:// 连接串，而各服务实际全部走 aiosqlite，属误导性死代码。
DEFAULT_STORAGE = {"engine": "sqlite"}


def _default_config() -> Dict[str, Any]:
    return {
        "secret_key": "",
        "ports": dict(DEFAULT_PORTS),
        "storage": dict(DEFAULT_STORAGE),
        "backends": {k: dict(v) for k, v in DEFAULT_BACKENDS.items()},
        "feishu": {"app_id": "", "app_secret": ""},
        "ai": {
            "deepseek_api_key": "",
            "gemini_api_key": "",
            "qwen_api_key": "",
            "proxy_url": "",
        },
        "data_dir": "data",
    }


def _deep_merge_defaults(cfg: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """把默认模板递归补进 cfg（只补缺失键，不覆盖已有值）。

    此前用 `cfg.setdefault(k, v)` 只补顶层：用户手改 config.json 时若写了
    `"backends": {"redis": {...}}` 却省略 postgres/qdrant，顶层 backends 已存在
    → 子键永远补不上 → `be.get("postgres")` 为 None → 后端被误判为 disabled。
    同时对默认值做 deepcopy，杜绝 cfg 与模板共享可变对象导致的跨实例污染。
    """
    for k, v in base.items():
        if k not in cfg or cfg[k] is None:
            cfg[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(cfg[k], dict):
            _deep_merge_defaults(cfg[k], v)
    return cfg


def load_config(path: Path | None = None) -> Dict[str, Any]:
    """加载配置；若不存在则从示例创建一份（含随机 secret_key）。

    敏感字段（AI Key / 飞书 Secret / secret_key）在盘上以 DPAPI 密文存储，
    这里透明解密成明文返回，调用方无感知。
    """
    path = path or CONFIG_PATH
    if not path.exists():
        cfg = _default_config()
        cfg["secret_key"] = secrets.token_urlsafe(32)
        save_config(cfg, path)
        print(f"[config] 已生成默认配置：{path}（secret_key 已随机生成）")
        return cfg
    cfg = json.loads(path.read_text(encoding="utf-8"))
    # 递归补齐缺失字段（含嵌套子键）
    _deep_merge_defaults(cfg, _default_config())
    # 判断「盘上」是否还有明文密钥 —— 必须在解密之前判断：
    # 解密后内存里本来就全是明文，此时再检测会永远为真、每次启动都重复迁移。
    disk_has_plaintext = secretstore.has_plaintext_secret(cfg)
    # 盘上密文 → 内存明文
    secretstore.decrypt_config(cfg)
    need_save = False
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_urlsafe(32)
        need_save = True
    # 一次性迁移：老配置的明文密钥升级为 DPAPI 密文
    if disk_has_plaintext:
        try:
            backup = path.with_suffix(".json.plain.bak")
            if not backup.exists():
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            need_save = True
            print(f"[config] 检测到明文密钥，已加密存储（原文件备份：{backup.name}）")
        except Exception:
            need_save = True
    if need_save:
        save_config(cfg, path)
    return cfg


def save_config(cfg: Dict[str, Any], path: Path | None = None) -> None:
    """写盘：敏感字段加密后落盘；传入的 cfg 保持明文不被修改。"""
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    to_write = secretstore.encrypt_config(cfg)
    path.write_text(json.dumps(to_write, ensure_ascii=False, indent=2), encoding="utf-8")


def service_urls(cfg: Dict[str, Any]) -> Dict[str, str]:
    """各服务彼此调用的内网地址（127.0.0.1 + 偏移端口）。"""
    p = cfg["ports"]
    return {
        "feishu_agent": f"http://127.0.0.1:{p['feishu_agent']}",
        "ai_router": f"http://127.0.0.1:{p['ai_router']}",
        "agent_pipeline": f"http://127.0.0.1:{p['agent_pipeline']}",
        "spider": f"http://127.0.0.1:{p['spider']}",
    }


def storage_engine(cfg: Dict[str, Any]) -> str:
    """当前存储引擎（桌面版恒为 sqlite；保留分支以便未来接回外部 PG）。"""
    return ((cfg.get("storage") or {}).get("engine") or "sqlite").lower()


def database_url(cfg: Dict[str, Any], data_dir: str | Path | None = None) -> str:
    """数据库连接串。

    桌面版全部走进程内 aiosqlite（各服务 `DATA_DIR/goofish.db`），因此这里
    如实返回 `sqlite+aiosqlite:///...`。**不要再伪造 postgresql:// 串**——
    此前那样写会让人（和未来的代码）误判本项目连的是 PostgreSQL，排障时
    白白去查 5439 端口，而实际上没有任何服务读取过该变量。
    """
    engine = storage_engine(cfg)
    if engine == "sqlite":
        if data_dir:
            p = Path(data_dir) / "goofish.db"
            return "sqlite+aiosqlite:///" + str(p).replace("\\", "/")
        return "sqlite+aiosqlite://"
    # 非 sqlite（预留：用户显式切外部 PG 时才生成真实 PG 串）
    pg = (cfg.get("backends") or {}).get("postgres") or {}
    return (f"postgresql://{pg.get('user','goofish')}:{pg.get('password','')}"
            f"@127.0.0.1:{pg.get('port', 5439)}/{pg.get('db','goofish_ai')}")


def backend_urls(cfg: Dict[str, Any], data_dir: str | Path | None = None) -> Dict[str, str]:
    """本地后端连接串（未启用时服务会优雅降级）。

    注意：redis/qdrant 在桌面版分别由 fakeredis / Chroma 进程内实现，
    URL 仅为历史兼容保留，无实际连接语义。
    """
    b = cfg.get("backends") or {}
    rd = b.get("redis") or {}
    qd = b.get("qdrant") or {}
    return {
        "DATABASE_URL": database_url(cfg, data_dir),
        "REDIS_URL": f"redis://127.0.0.1:{rd.get('port', 6399)}/0",
        "QDRANT_URL": f"http://127.0.0.1:{qd.get('port', 6339)}",
    }
