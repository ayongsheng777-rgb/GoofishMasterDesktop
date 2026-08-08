# -*- coding: utf-8 -*-
"""common/keyword_expander.py 单元测试：瑕疵定义词 × 设备名词双向展开。"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.keyword_expander import expand_keyword  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KEYWORD_EXPAND_ENABLED", raising=False)
    monkeypatch.delenv("KEYWORD_EXPAND_MAX", raising=False)


class TestNoExpansion:
    def test_plain_device_unchanged(self):
        assert expand_keyword("iphone 15") == ["iphone 15"]

    def test_unrelated_keyword_unchanged(self):
        assert expand_keyword("数据线") == ["数据线"]

    def test_empty_returns_empty(self):
        assert expand_keyword("") == []
        assert expand_keyword("   ") == []

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("KEYWORD_EXPAND_ENABLED", "false")
        assert expand_keyword("摔坏的手机") == ["摔坏的手机"]


class TestBareDefect:
    def test_pairs_with_default_devices(self):
        out = expand_keyword("坏的")
        assert out == ["坏的手机", "坏的iphone", "坏的笔记本电脑", "坏的平板"]

    def test_glue_de_added_when_missing(self):
        out = expand_keyword("摔坏", max_variants=2)
        assert out == ["摔坏的手机", "摔坏的iphone"]

    def test_original_bare_word_dropped(self):
        # 裸瑕疵词单独搜索全是噪音，不应保留在变体里
        out = expand_keyword("变形")
        assert "变形" not in out
        assert all("变形" in v for v in out)


class TestDefectPlusDevice:
    def test_original_first(self):
        out = expand_keyword("摔坏的手机")
        assert out[0] == "摔坏的手机"

    def test_both_axes_covered(self):
        out = expand_keyword("摔坏的手机")
        # 设备轴：iphone 变体；瑕疵轴：其他瑕疵词变体
        assert any("iphone" in v for v in out)
        assert any(v != "摔坏的手机" and "手机" in v and "摔坏" not in v
                   for v in out)

    def test_cap_default_4(self):
        assert len(expand_keyword("摔坏的手机")) == 4

    def test_cap_configurable(self):
        assert len(expand_keyword("摔坏的手机", max_variants=6)) == 6
        assert len(expand_keyword("摔坏的手机", max_variants=1)) == 1

    def test_env_cap(self, monkeypatch):
        monkeypatch.setenv("KEYWORD_EXPAND_MAX", "3")
        assert len(expand_keyword("屏幕破的笔记本电脑")) == 3

    def test_no_de_glue_preserved(self):
        # 原词无「的」→ 变体也不强行加「的」
        out = expand_keyword("摔坏手机", max_variants=2)
        assert out[0] == "摔坏手机"
        assert out[1] == "摔坏iphone"

    def test_suffix_preserved(self):
        out = expand_keyword("摔坏的手机 64g", max_variants=2)
        assert out[0] == "摔坏的手机 64g"
        assert out[1].endswith(" 64g")
        assert "iphone" in out[1]

    def test_case_insensitive(self):
        out = expand_keyword("摔坏的IPHONE", max_variants=2)
        assert out[0] == "摔坏的iphone"

    def test_longest_device_form_wins(self):
        # 「苹果手机」应整体匹配，而不是只匹配到「手机」
        out = expand_keyword("坏的苹果手机", max_variants=2)
        assert out[0] == "坏的苹果手机"
        assert out[1] == "坏的手机" or "iphone" in out[1]

    def test_screen_family(self):
        out = expand_keyword("屏幕破的笔记本电脑")
        assert out[0] == "屏幕破的笔记本电脑"
        assert any("macbook" in v or "笔记本" in v for v in out[1:])

    def test_dedup(self):
        out = expand_keyword("进水的相机")
        assert len(out) == len(set(out))
