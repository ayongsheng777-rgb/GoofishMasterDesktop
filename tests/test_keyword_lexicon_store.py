# -*- coding: utf-8 -*-
"""学习词库（keyword_lexicon_store）+ expander 合并层测试。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import keyword_lexicon_store as store  # noqa: E402
from common.keyword_expander import (  # noqa: E402
    DEFECT_FAMILIES, classify_keyword, expand_keyword, extract_residual)

BUILTIN_FORMS = {f for _c, forms in DEFECT_FAMILIES for f in forms}


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("KEYWORD_LEXICON_PATH",
                       str(tmp_path / "keyword_lexicon.json"))
    monkeypatch.delenv("KEYWORD_EXPAND_ENABLED", raising=False)
    monkeypatch.delenv("KEYWORD_EXPAND_MAX", raising=False)
    store._CACHE.update(mtime=None, data=None)
    yield
    store._CACHE.update(mtime=None, data=None)


class TestNewBuiltinFamilies:
    def test_keyboard_failure(self):
        out = expand_keyword("键盘不能用的笔记本")
        assert out[0] == "键盘不能用的笔记本"
        assert any("macbook" in v for v in out)

    def test_air_leak_paddleboard(self):
        out = expand_keyword("跑气的桨板")
        assert out[0] == "跑气的桨板"
        assert any("冲浪板" in v or "充气" in v for v in out[1:])

    def test_patched_paddleboard(self):
        out = expand_keyword("补了一块的桨板", max_variants=2)
        assert out[0] == "补了一块的桨板"

    def test_screen_spots(self):
        out = expand_keyword("屏幕有黑点的手机", max_variants=2)
        assert out[0] == "屏幕有黑点的手机"
        assert "iphone" in out[1]

    def test_weak_form_without_device_no_expand(self):
        # 「条纹衬衫」「护手霜」类歧义词：无设备词时不触发裸词配对
        assert expand_keyword("条纹衬衫") == ["条纹衬衫"]
        assert classify_keyword("条纹衬衫")["defect"] is None

    def test_weak_form_with_device_expands(self):
        out = expand_keyword("划痕的iphone", max_variants=2)
        assert len(out) == 2
        assert out[0] == "划痕的iphone"


class TestAiLearnedTerms:
    def test_ai_terms_merge_into_expansion(self):
        assert expand_keyword("屏幕有绿线的手机") == ["屏幕有绿线的手机"]
        store.add_ai_defect_terms(["屏幕有绿线"])
        out = expand_keyword("屏幕有绿线的手机")
        assert len(out) > 1
        assert out[0] == "屏幕有绿线的手机"
        assert any("iphone" in v for v in out)

    def test_ai_term_family_inference(self):
        store.add_ai_defect_terms(["慢漏气", "起皮"])
        fams = dict(store.learned_defect_families())
        assert "慢漏气" in fams.get("漏气", [])
        assert any("起皮" in terms for terms in fams.values())

    def test_short_or_duplicate_terms_rejected(self):
        assert store.add_ai_defect_terms(["x", "坏" * 20, "摔坏"]) == []
        assert store.learned_defect_families() == [] or all(
            "摔坏" not in terms for _f, terms in store.learned_defect_families())

    def test_extract_residual(self):
        assert extract_residual("键盘不能用的笔记本") == "键盘不能用"
        assert extract_residual("跑气的桨板") == "跑气"


class TestMiningAndReinforce:
    def test_candidate_promoted_after_two_sightings(self):
        known = set(BUILTIN_FORMS) | store.all_known_defect_forms()
        store.learn_from_titles(["商品标题：转轴松动 9 成新"], known, True)
        assert classify_keyword("转轴松动的笔记本")["defect"] is None  # 还没转正
        stats = store.learn_from_titles(["另一台 转轴松动 便宜出"], known, True)
        assert stats["promoted"] >= 1
        # 转正后静态命中
        assert classify_keyword("转轴松动的笔记本")["defect"] == "转轴松动"

    def test_no_mining_without_defect_context(self):
        store.learn_from_titles(["转轴松动 便宜出"], set(BUILTIN_FORMS), False)
        assert store.load()["candidates"] == {}

    def test_hit_reinforcement(self):
        store.add_ai_defect_terms(["屏幕有绿线"])
        store.learn_from_titles(["手机屏幕有绿线 便宜出"], set(BUILTIN_FORMS), False)
        entries = store.load()["learned_defects"]["屏幕瑕疵"]
        assert entries[0]["hits"] == 2  # 入库 1 + 强化 1

    def test_composite_phrase_with_known_form_rejected(self):
        """回归：「摔坏的手机」= 已知瑕疵词+设备词，学进去会吞设备位，
        展开出「摔坏的手机的手机」（压测实锤的污染 bug）"""
        known = set(BUILTIN_FORMS) | store.all_known_defect_forms()
        for _ in range(3):  # 出现多次也不得转正
            store.learn_from_titles(["共用语 摔坏的手机"], known, True)
        assert "摔坏的手机" not in {
            t for _f, ts in store.learned_defect_families() for t in ts}
        # 展开结构不被破坏：设备位仍是「手机」
        assert expand_keyword("摔坏的手机")[0] == "摔坏的手机"

    def test_cache_isolated_by_path(self, tmp_path, monkeypatch):
        """回归：load() 缓存按路径隔离——文件不存在时不得返回
        另一个路径的旧缓存（压测实锤的跨测试污染）"""
        store.set_ai_variants("泡水", ["泡水的手机"])  # 写入路径 A
        monkeypatch.setenv("KEYWORD_LEXICON_PATH",
                           str(tmp_path / "other" / "lex.json"))  # 切到路径 B
        assert store.get_ai_variants("泡水") is None  # 拿不到 A 的数据
        assert store.learned_defect_families() == []


class TestAiCache:
    def test_roundtrip(self):
        assert store.get_ai_variants("屏幕有绿线的手机") is None
        store.set_ai_variants("屏幕有绿线的手机", ["屏幕有绿线的手机", "绿线的手机"])
        assert store.get_ai_variants("屏幕有绿线的手机") == [
            "屏幕有绿线的手机", "绿线的手机"]

    def test_negative_cache(self):
        store.set_ai_variants("iphone 15", [])
        assert store.get_ai_variants("iphone 15") == []

    def test_persistence_across_cache_flush(self):
        store.set_ai_variants("泡水", ["泡水的手机"])
        store._CACHE.update(mtime=None, data=None)  # 模拟另一进程冷读
        assert store.get_ai_variants("泡水") == ["泡水的手机"]
