# -*- coding: utf-8 -*-
"""common/config.py —— 配置深合并、存储语义、加密往返。

回归的三个真实故障：
1. `setdefault` 只补顶层键 → 用户手改 config.json 省略 backends.postgres 后
   该后端被误判 disabled。
2. `backend_urls()` 生成 postgresql:// 串，而全部服务实际走 aiosqlite → 排障
   时被误导去查 5439 端口。
3. 默认模板与 cfg 共享可变对象 → 改一份实例污染另一份。
"""
from __future__ import annotations

import json

from common import config as cfg_mod


# ---------- 深合并 ----------

def test_deep_merge_fills_missing_nested_keys():
    """顶层键已存在时，缺失的子键也必须补上（原 setdefault 的漏洞）。"""
    user = {"backends": {"redis": {"enabled": False, "port": 1234}}}
    merged = cfg_mod._deep_merge_defaults(user, cfg_mod._default_config())

    # 用户没写的兄弟键被补齐
    assert merged["backends"]["postgres"]["enabled"] is True
    assert merged["backends"]["qdrant"]["port"] == 6339
    # 用户写了的值原样保留，不被默认值覆盖
    assert merged["backends"]["redis"]["enabled"] is False
    assert merged["backends"]["redis"]["port"] == 1234
    # 顶层缺失键也补齐
    assert merged["ports"]["feishu_agent"] == 8911
    assert merged["ai"]["proxy_url"] == ""


def test_deep_merge_treats_none_as_missing():
    """显式写成 null 的字段等同缺失——否则后续 .get() 链会炸 NoneType。"""
    user = {"ai": None, "ports": {"spider": None}}
    merged = cfg_mod._deep_merge_defaults(user, cfg_mod._default_config())
    assert isinstance(merged["ai"], dict)
    assert merged["ports"]["spider"] == 8914


def test_deep_merge_does_not_share_mutable_defaults():
    """两次合并出来的实例必须互不影响（deepcopy 而非引用）。"""
    a = cfg_mod._deep_merge_defaults({}, cfg_mod._default_config())
    b = cfg_mod._deep_merge_defaults({}, cfg_mod._default_config())
    a["backends"]["postgres"]["port"] = 19999
    assert b["backends"]["postgres"]["port"] == 5439
    # 也不能污染模板本身
    assert cfg_mod.DEFAULT_BACKENDS["postgres"]["port"] == 5439


# ---------- 存储语义 ----------

def test_database_url_is_sqlite_by_default(tmp_path):
    """桌面版必须如实返回 sqlite 串，不得伪造 postgresql://。"""
    cfg = cfg_mod._default_config()
    url = cfg_mod.database_url(cfg, tmp_path)
    assert url.startswith("sqlite+aiosqlite:///")
    assert "goofish.db" in url
    assert "postgresql" not in url
    # Windows 反斜杠必须转成正斜杠，否则 SQLAlchemy 解析出错
    assert "\\" not in url


def test_database_url_switches_to_pg_when_engine_overridden():
    """预留分支：显式切外部 PG 时才生成真实 PG 串。"""
    cfg = cfg_mod._default_config()
    cfg["storage"]["engine"] = "postgres"
    url = cfg_mod.database_url(cfg)
    assert url.startswith("postgresql://")
    assert ":5439/" in url


def test_storage_engine_defaults_to_sqlite_on_garbage():
    assert cfg_mod.storage_engine({}) == "sqlite"
    assert cfg_mod.storage_engine({"storage": {}}) == "sqlite"
    assert cfg_mod.storage_engine({"storage": {"engine": "SQLite"}}) == "sqlite"


def test_backend_urls_uses_database_url(tmp_path):
    cfg = cfg_mod._default_config()
    urls = cfg_mod.backend_urls(cfg, tmp_path)
    assert urls["DATABASE_URL"] == cfg_mod.database_url(cfg, tmp_path)
    assert urls["REDIS_URL"].startswith("redis://127.0.0.1:6399")
    assert urls["QDRANT_URL"].startswith("http://127.0.0.1:6339")


# ---------- 落盘 / 加载 ----------

def test_load_creates_default_with_random_secret(tmp_cfg_path):
    cfg = cfg_mod.load_config(tmp_cfg_path)
    assert tmp_cfg_path.exists()
    assert len(cfg["secret_key"]) >= 32
    # 两次生成的 secret 不能相同
    other = tmp_cfg_path.parent / "other.json"
    cfg2 = cfg_mod.load_config(other)
    assert cfg["secret_key"] != cfg2["secret_key"]


def test_secrets_are_not_stored_in_plaintext(tmp_cfg_path):
    """写盘后，原始 Key 不得以明文出现在文件里（Windows DPAPI 生效时）。"""
    from common import secretstore

    cfg = cfg_mod._default_config()
    cfg["secret_key"] = "unit-test-secret-key"
    cfg["ai"]["deepseek_api_key"] = "sk-unittest-abcdefghijklmnop"
    cfg_mod.save_config(cfg, tmp_cfg_path)

    raw = tmp_cfg_path.read_text(encoding="utf-8")
    if secretstore.available():
        assert "sk-unittest-abcdefghijklmnop" not in raw
        assert "enc:v1:" in raw
    # 非 Windows 平台透传明文，只断言可回读
    back = cfg_mod.load_config(tmp_cfg_path)
    assert back["ai"]["deepseek_api_key"] == "sk-unittest-abcdefghijklmnop"
    assert back["secret_key"] == "unit-test-secret-key"


def test_save_config_does_not_mutate_caller_dict(tmp_cfg_path):
    """加密只作用于落盘副本，调用方手里的 cfg 必须仍是明文。"""
    cfg = cfg_mod._default_config()
    cfg["ai"]["qwen_api_key"] = "sk-caller-should-stay-plain"
    cfg_mod.save_config(cfg, tmp_cfg_path)
    assert cfg["ai"]["qwen_api_key"] == "sk-caller-should-stay-plain"


def test_plaintext_migration_runs_once(tmp_cfg_path, capsys):
    """老配置（明文密钥）首次加载迁移并备份，之后不得反复提示。"""
    from common import secretstore

    tmp_cfg_path.write_text(json.dumps({
        "secret_key": "legacy-plain-secret",
        "ai": {"deepseek_api_key": "sk-legacy-plaintext-key-1234"},
    }, ensure_ascii=False), encoding="utf-8")

    cfg_mod.load_config(tmp_cfg_path)
    first = capsys.readouterr().out

    cfg_mod.load_config(tmp_cfg_path)
    second = capsys.readouterr().out

    if secretstore.available():
        assert "检测到明文密钥" in first
        assert "检测到明文密钥" not in second, "迁移提示重复触发（解密顺序回归）"
        assert tmp_cfg_path.with_suffix(".json.plain.bak").exists()

    # 无论平台，值都必须能完整回读
    cfg = cfg_mod.load_config(tmp_cfg_path)
    assert cfg["ai"]["deepseek_api_key"] == "sk-legacy-plaintext-key-1234"
    assert cfg["secret_key"] == "legacy-plain-secret"
