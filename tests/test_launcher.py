# -*- coding: utf-8 -*-
"""launcher.py —— 启动编排的纯逻辑部分。

覆盖两个真实事故：
1. 硬编码启动顺序把 feishu-agent 排在 agent-pipeline 前面 → feishu-agent
   刚起来就转发指令给尚未监听的 :8913 → connection refused。
2. `_safe_env` 早期实现对超长 PATH 直接 `v[:limit]` 硬切 → 末尾留下半截路径
   （"C:\\Program Files\\Pyt"），子进程找不到 python/playwright，
   报错信息完全不指向真因。

导入 launcher 会执行模块级 `cfg_mod.load_config()`，为免污染真实 config.json，
这里把 APP_DIR 指到临时目录后再导入。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def launcher():
    """在隔离的 APP_DIR 下导入 launcher（避免动到用户真实配置）。"""
    tmp = Path(tempfile.mkdtemp(prefix="gmd_launcher_test_"))
    from common import config as cfg_mod
    orig_app_dir, orig_cfg_path = cfg_mod.APP_DIR, cfg_mod.CONFIG_PATH
    cfg_mod.APP_DIR = tmp
    cfg_mod.CONFIG_PATH = tmp / "config" / "config.json"
    sys.modules.pop("launcher", None)
    import launcher as mod
    yield mod
    cfg_mod.APP_DIR, cfg_mod.CONFIG_PATH = orig_app_dir, orig_cfg_path
    sys.modules.pop("launcher", None)


# ---------- 拓扑排序 ----------

def test_default_order_starts_dependencies_first(launcher):
    """真实服务定义必须排出「被调用方先起」的顺序。"""
    names = [s["name"] for s in launcher.SERVICES]
    assert names.index("ai-router") < names.index("agent-pipeline")
    assert names.index("agent-pipeline") < names.index("feishu-agent")
    assert names.index("spider-service") < names.index("feishu-agent")
    assert len(names) == 4


def test_topo_sort_respects_declared_dependencies(launcher):
    defs = [
        {"name": "c", "depends": ["b"]},
        {"name": "a", "depends": []},
        {"name": "b", "depends": ["a"]},
    ]
    assert [d["name"] for d in launcher._topo_sort(defs)] == ["a", "b", "c"]


def test_topo_sort_is_stable_within_same_layer(launcher):
    """同层内保持声明顺序，保证每次启动次序可复现（便于排障对日志）。"""
    defs = [{"name": n, "depends": []} for n in ("x", "y", "z")]
    assert [d["name"] for d in launcher._topo_sort(defs)] == ["x", "y", "z"]


def test_topo_sort_falls_back_on_cycle(launcher):
    """有环时必须回退声明顺序，绝不能少启动服务或死循环。"""
    defs = [
        {"name": "a", "depends": ["b"]},
        {"name": "b", "depends": ["a"]},
    ]
    out = launcher._topo_sort(defs)
    assert [d["name"] for d in out] == ["a", "b"]


def test_topo_sort_ignores_unknown_dependency(launcher):
    """依赖了不存在的服务名时忽略该依赖，而不是把它当环卡死。"""
    defs = [
        {"name": "a", "depends": ["ghost"]},
        {"name": "b", "depends": ["a"]},
    ]
    assert [d["name"] for d in launcher._topo_sort(defs)] == ["a", "b"]


# ---------- 环境变量裁剪 ----------

def test_truncate_path_like_never_produces_partial_entry(launcher):
    entries = [rf"C:\some\dir{i:05d}\bin" for i in range(3000)]
    value = os.pathsep.join(entries)
    assert len(value) > launcher._ENV_VALUE_LIMIT

    out = launcher._truncate_path_like("PATH", value)
    assert len(out) <= launcher._ENV_VALUE_LIMIT

    kept = out.split(os.pathsep)
    # 每一条保留下来的都必须是原始条目的完整拷贝
    assert set(kept).issubset(set(entries))
    # 保持原有顺序（前缀），不能乱序
    assert kept == entries[:len(kept)]
    assert len(kept) < len(entries)  # 确实丢了东西


def test_truncate_path_like_keeps_short_value_intact(launcher):
    value = os.pathsep.join([r"C:\a", r"C:\b"])
    assert launcher._truncate_path_like("PATH", value) == value


def test_safe_env_truncates_oversized_and_keeps_normal(launcher, monkeypatch):
    """不能用 monkeypatch.setenv 造超长值 —— Windows 的 os.environ 自己就会抛
    'the environment variable is longer than 32767 characters'（这恰恰证明了
    _safe_env 的必要性）。改为直接替换 launcher 视野里的 os.environ。
    """
    fake_env = {
        "GMD_HUGE": "x" * (launcher._ENV_VALUE_LIMIT + 500),
        "PATH": os.pathsep.join(rf"C:\p{i:05d}" for i in range(4000)),
        "GMD_NORMAL": "hello",
    }
    monkeypatch.setattr(launcher.os, "environ", fake_env)

    env = launcher._safe_env()
    # 非路径型：硬截断到上限
    assert len(env["GMD_HUGE"]) == launcher._ENV_VALUE_LIMIT
    # 普通值原样保留
    assert env["GMD_NORMAL"] == "hello"
    # 路径型：按条目裁剪，每条都完整
    kept = env["PATH"].split(os.pathsep)
    assert all(len(p) == len(r"C:\p00000") for p in kept), "出现了半截路径"
    # 所有值都必须在限内
    assert all(len(v) <= launcher._ENV_VALUE_LIMIT for v in env.values())


# ---------- 环境注入 ----------

@pytest.mark.parametrize("svc_name", ["ai-router", "agent-pipeline",
                                      "spider-service", "feishu-agent"])
def test_build_env_injects_storage_and_database_url(launcher, svc_name):
    """文档一直声称注入 DATABASE_URL，实际历史上只注入了 POSTGRES_URL。"""
    env = launcher.build_env(svc_name)
    assert env["STORAGE_ENGINE"] == "sqlite"
    assert env["DATABASE_URL"].startswith("sqlite+aiosqlite:///")
    assert "postgresql://" not in env["DATABASE_URL"]
    # SQLITE_DISABLED 必须存在且为假值——ai-router/agent-pipeline 靠它决定
    # 是否启用内嵌库（此前 ai-router 误读 POSTGRES_ENABLED，关 PG 会连带关库）
    assert env["SQLITE_DISABLED"].lower() in ("false", "0", "no", "off")


def test_build_env_sqlite_survives_postgres_disabled(launcher, monkeypatch):
    """把 postgres 后端关掉时，SQLITE_DISABLED 仍必须是 false（BUG-6 回归）。"""
    monkeypatch.setitem(launcher.CFG["backends"]["postgres"], "enabled", False)
    env = launcher.build_env("ai-router")
    assert env["POSTGRES_ENABLED"] == "false"
    assert env["SQLITE_DISABLED"] == "false"


def test_build_env_service_urls_point_to_localhost(launcher):
    env = launcher.build_env("feishu-agent")
    for key in ("AI_ROUTER_URL", "PIPELINE_URL", "SPIDER_URL"):
        assert env[key].startswith("http://127.0.0.1:")


def test_build_env_port_matches_service(launcher):
    """端口必须与配置一致（8911-8914），错位会导致服务互相打不通。"""
    expected = {"feishu-agent": "feishu_agent", "ai-router": "ai_router",
                "agent-pipeline": "agent_pipeline", "spider-service": "spider"}
    for svc, key in expected.items():
        assert launcher.build_env(svc)["PORT"] == str(launcher.CFG["ports"][key])
