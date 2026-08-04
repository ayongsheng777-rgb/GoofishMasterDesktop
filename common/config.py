# -*- coding: utf-8 -*-
"""
GoofishMasterDesktop 配置中心
读取 config/config.json，提供端口映射、后端地址、AI Key 等。
与原 Docker 项目完全解耦：端口整体偏移 +10（8911-8914），后端地址指向 127.0.0.1。
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict

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


def _default_config() -> Dict[str, Any]:
    return {
        "secret_key": "",
        "ports": dict(DEFAULT_PORTS),
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


def load_config(path: Path | None = None) -> Dict[str, Any]:
    """加载配置；若不存在则从示例创建一份（含随机 secret_key）。"""
    path = path or CONFIG_PATH
    if not path.exists():
        cfg = _default_config()
        cfg["secret_key"] = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[config] 已生成默认配置：{path}（secret_key 已随机生成）")
        return cfg
    cfg = json.loads(path.read_text(encoding="utf-8"))
    # 补齐缺失字段
    base = _default_config()
    for k, v in base.items():
        cfg.setdefault(k, v)
    cfg["ports"].setdefault("feishu_agent", DEFAULT_PORTS["feishu_agent"])
    cfg["ports"].setdefault("ai_router", DEFAULT_PORTS["ai_router"])
    cfg["ports"].setdefault("agent_pipeline", DEFAULT_PORTS["agent_pipeline"])
    cfg["ports"].setdefault("spider", DEFAULT_PORTS["spider"])
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_urlsafe(32)
        save_config(cfg, path)
    return cfg


def save_config(cfg: Dict[str, Any], path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def service_urls(cfg: Dict[str, Any]) -> Dict[str, str]:
    """各服务彼此调用的内网地址（127.0.0.1 + 偏移端口）。"""
    p = cfg["ports"]
    return {
        "feishu_agent": f"http://127.0.0.1:{p['feishu_agent']}",
        "ai_router": f"http://127.0.0.1:{p['ai_router']}",
        "agent_pipeline": f"http://127.0.0.1:{p['agent_pipeline']}",
        "spider": f"http://127.0.0.1:{p['spider']}",
    }


def backend_urls(cfg: Dict[str, Any]) -> Dict[str, str]:
    """本地后端连接串（未启动时服务会优雅降级）。"""
    b = cfg["backends"]
    pg = b["postgres"]
    rd = b["redis"]
    qd = b["qdrant"]
    return {
        "DATABASE_URL": f"postgresql://{pg['user']}:{pg['password']}@127.0.0.1:{pg['port']}/{pg['db']}",
        "REDIS_URL": f"redis://127.0.0.1:{rd['port']}/0",
        "QDRANT_URL": f"http://127.0.0.1:{qd['port']}",
    }
