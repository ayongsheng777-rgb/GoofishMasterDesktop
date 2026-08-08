# -*- coding: utf-8 -*-
"""发布时间设定测试：指令解析「最近N天」+ spider 侧二次过滤/选项映射。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_command_parser():
    """feishu-agent 目录名带连字符不能直接 import，按文件路径加载。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "command_parser", ROOT / "services" / "feishu-agent" / "command_parser.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


command_parser = _load_command_parser()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KEYWORD_EXPAND_ENABLED", raising=False)


def _search_cmd(text: str) -> dict:
    return command_parser.parse_command(text)


class TestCommandParsing:
    def test_recent_n_days(self):
        cmd = _search_cmd("找 摔坏的手机 最近3天")
        assert cmd.get("publish_within_days") == 3
        assert "最近" not in cmd["keyword"]
        assert "3天" not in cmd["keyword"]

    def test_within_n_days_publish(self):
        cmd = _search_cmd("监控 iphone15 7天内发布")
        assert cmd.get("publish_within_days") == 7
        assert "7天" not in cmd["keyword"]

    def test_clamped_to_14(self):
        cmd = _search_cmd("找 macbook 最近30天")
        assert cmd.get("publish_within_days") == 14

    def test_no_time_phrase_no_field(self):
        cmd = _search_cmd("找 iphone15")
        assert not cmd.get("publish_within_days")

    def test_time_with_price_and_pages(self):
        cmd = _search_cmd("找 RTX4090 低于8000 3页 最近1天")
        assert cmd.get("publish_within_days") == 1
        assert cmd.get("max_price") == 8000
        assert cmd.get("max_pages") == 3
        assert cmd["keyword"] == "RTX4090"


class TestSpiderFilter:
    """spider main.py 的纯函数（直接 import 会拉起 FastAPI，改为源码内嵌验证）。"""

    def _load_funcs(self):
        # spider main.py 依赖重（playwright 等），测试环境从源码提取纯函数执行
        import re
        src = (ROOT / "services" / "spider-service" / "main.py").read_text(
            encoding="utf-8")
        ns: dict = {"Optional": __import__("typing").Optional,
                    "List": __import__("typing").List,
                    "Dict": __import__("typing").Dict,
                    "Any": __import__("typing").Any}
        for fname in ("_publish_option_for_days", "_filter_by_publish_time"):
            m = re.search(rf"\ndef {fname}\(.*?(?=\n(?:def |async def |# |@))",
                          src, re.S)
            assert m, f"{fname} not found in spider main.py"
            exec(m.group(0), ns)
        return ns["_publish_option_for_days"], ns["_filter_by_publish_time"]

    def test_option_mapping(self):
        opt, _f = self._load_funcs()
        assert opt(1) == "一天内"
        assert opt(2) == "三天内"
        assert opt(3) == "三天内"
        assert opt(7) == "七天内"
        assert opt(14) == "十四天内"

    def test_filter_drops_old_keeps_recent(self):
        _o, filt = self._load_funcs()
        now = datetime.now()
        items = [
            {"title": "a", "publish_time": (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")},
            {"title": "b", "publish_time": (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M")},
            {"title": "c", "publish_time": "未知时间"},
            {"title": "d", "publish_time": ""},
        ]
        kept, dropped = filt(items, 3)
        assert [i["title"] for i in kept] == ["a", "c", "d"]  # 未知时间保留
        assert dropped == 1
