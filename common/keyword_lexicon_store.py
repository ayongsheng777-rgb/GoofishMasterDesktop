# -*- coding: utf-8 -*-
"""学习词库存储：让瑕疵关键词库像 AI 记忆一样增量进化。

三层来源（见 common/keyword_expander.py 顶部说明）：
1. 内置词库（代码静态维护）——快速、零成本；
2. AI 学习词（source=ai）——静态词库未命中时 spider 调 ai-router 理解
   关键词，AI 给出的瑕疵表述落盘，下次同词直接静态命中，不再烧 token；
3. 挖掘词（source=mined）——瑕疵类搜索结果标题里反复出现（≥2 次）的
   未知损坏表述自动转正入库。

另有命中强化：学习词在采集标题中每出现一次 hits+1，可在管理界面展示
「词库学到了什么、哪些词真的有用」。

存储：单个 JSON 文件（默认 <项目根>/data/keyword_lexicon.json，可用
KEYWORD_LEXICON_PATH 或 DATA_DIR 环境变量改址）。原子写（tmp+replace），
读取按 mtime 缓存——多进程（spider 写 / pipeline 读）场景下安全。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {"mtime": None, "data": None}

# 挖掘窗口：损坏信号字前后各取 3 个汉字作为候选表述
_SIGNAL_CHARS = "坏碎裂漏破弯压扁泡淹洒淋潮磕摔划斑纹印气灵鼓锈霉焦烧松"
_MINE_PATTERN = re.compile(
    r"[一-鿿]{0,3}[%s][一-鿿]{0,3}" % _SIGNAL_CHARS)
_MINED_MIN_LEN, _MINED_MAX_LEN = 2, 6
_PROMOTE_HITS = 2           # 候选词出现这么多次即转正
_MAX_CANDIDATES = 500       # 候选池上限，防无限膨胀
_AI_NEGATIVE_TTL = 7 * 86400  # AI 判「非瑕疵」的缓存有效期


def lexicon_path() -> Path:
    """词库文件路径：显式 env > DATA_DIR 同级 > 项目根 data/。"""
    env = os.environ.get("KEYWORD_LEXICON_PATH")
    if env:
        return Path(env)
    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        # DATA_DIR 通常指向 <root>/data/spider，词库放其上一级
        return Path(data_dir).resolve().parent / "keyword_lexicon.json"
    return Path(__file__).resolve().parents[1] / "data" / "keyword_lexicon.json"


def _empty() -> Dict[str, Any]:
    return {"version": 1, "learned_defects": {}, "learned_devices": {},
            "candidates": {}, "ai_cache": {}}


def load() -> Dict[str, Any]:
    """读取词库（按路径+mtime 缓存；文件不存在/损坏返回空结构）。

    缓存必须按路径隔离：调用方可能通过 KEYWORD_LEXICON_PATH/DATA_DIR
    切换词库文件（多实例/测试），否则 A 路径的旧数据会泄漏给 B 路径。
    """
    path = lexicon_path()
    spath = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        with _LOCK:
            if _CACHE["data"] is None or _CACHE.get("path") != spath:
                _CACHE.update(data=_empty(), path=spath, mtime=None)
            return _CACHE["data"]
    with _LOCK:
        if (_CACHE.get("path") != spath or _CACHE["mtime"] != mtime
                or _CACHE["data"] is None):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                base = _empty()
                base.update({k: v for k, v in data.items() if k in base})
                _CACHE["data"] = base
            except Exception:
                _CACHE["data"] = _empty()
            _CACHE.update(path=spath, mtime=mtime)
        return _CACHE["data"]


def _save(data: Dict[str, Any]) -> None:
    path = lexicon_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)
    with _LOCK:
        _CACHE["data"] = data
        _CACHE.update(path=str(path), mtime=path.stat().st_mtime)


# ---------------------------------------------------------------------------
# 查询：供 expander 合并
# ---------------------------------------------------------------------------

def learned_defect_families() -> List[tuple]:
    """学习到的瑕疵词，按族组织：[(family, [term, ...]), ...]"""
    out = []
    for fam, entries in (load().get("learned_defects") or {}).items():
        terms = [e["term"] for e in entries if e.get("term")]
        if terms:
            out.append((fam, terms))
    return out


def learned_device_families() -> List[tuple]:
    out = []
    for fam, terms in (load().get("learned_devices") or {}).items():
        if terms:
            out.append((fam, list(terms)))
    return out


def all_known_defect_forms() -> set:
    forms = set()
    for _fam, entries in (load().get("learned_defects") or {}).items():
        forms.update(e["term"] for e in entries if e.get("term"))
    return forms


# ---------------------------------------------------------------------------
# 写入：AI 学习 / 挖掘 / 强化
# ---------------------------------------------------------------------------

def infer_family(term: str) -> str:
    """按特征字把新词归入最可能的瑕疵族（粗规则，错了也不致命——
    族只影响跨族展开顺序，不影响匹配）。注意顺序：屏幕瑕疵先于屏幕破
    （「屏幕有绿线」含「屏」但应归瑕疵族）。"""
    if "气" in term:
        return "漏气"
    if any(c in term for c in "漏泡淹洒淋潮渗湿"):
        return "进水"
    if any(c in term for c in "划斑纹印点裂黄线"):
        return "屏幕瑕疵"
    if any(c in term for c in "碎爆屏"):
        return "屏幕破"
    if any(c in term for c in "弯压扁挤"):
        return "变形"
    if any(c in term for c in "灵键充鼓扇响松"):
        return "部件失灵"
    if any(c in term for c in "补粘焊"):
        return "修补"
    return "坏"


def _builtin_defect_forms() -> set:
    """内置词库全部词形（延迟导入避免与 expander 循环依赖）。"""
    try:
        from common.keyword_expander import DEFECT_FAMILIES
        return {f for _c, forms in DEFECT_FAMILIES for f in forms}
    except Exception:
        return set()


def _all_device_forms() -> set:
    try:
        from common.keyword_expander import DEVICE_FAMILIES
        forms = {f for _c, fs in DEVICE_FAMILIES for f in fs}
    except Exception:
        forms = set()
    for _fam, terms in (load().get("learned_devices") or {}).items():
        forms.update(terms)
    return forms


def _is_composite_query(term: str, known_forms: set) -> bool:
    """「已知瑕疵词 + 设备词」的复合 query（如「摔坏的手机」）不应学为瑕疵词——
    学进去匹配时会吞掉设备位，展开出「摔坏的手机的手机」。
    但「慢漏气」= 已知词形+修饰语，是合法新表述，放行。"""
    devices = _all_device_forms()
    for k in known_forms:
        if len(k) < 2 or k not in term:
            continue
        residual = term.replace(k, "").strip("的了 ")
        if residual in devices:
            return True
    return False


def add_ai_defect_terms(terms: List[str]) -> List[str]:
    """AI 学到的瑕疵表述立即转正入库。返回实际新增的词。"""
    added: List[str] = []
    if not terms:
        return added
    with _LOCK:
        data = load()
        known = all_known_defect_forms() | _builtin_defect_forms()
        defects = data["learned_defects"]
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for term in terms:
            term = (term or "").strip()
            if not (_MINED_MIN_LEN <= len(term) <= 8) or term in known:
                continue
            # 复合 query（已知瑕疵词+设备词）不入库；修饰性新词形放行
            if _is_composite_query(term, known):
                continue
            fam = infer_family(term)
            defects.setdefault(fam, []).append({
                "term": term, "source": "ai", "hits": 1,
                "first_seen": now, "last_seen": now})
            known.add(term)
            added.append(term)
        if added:
            _save(data)
    return added


def learn_from_titles(titles: List[str], known_forms: set,
                      defect_context: bool) -> Dict[str, int]:
    """采集结果反哺（每次搜索后调用）：

    - 命中强化：已入库的学习词在标题中出现 → hits+1；
    - 挖掘：defect_context=True（本轮是瑕疵搜索）时，从标题提取未知
      损坏表述进候选池，累计 ≥2 次自动转正（source=mined）。

    返回 {"reinforced": n, "promoted": m} 供日志。
    """
    if not titles:
        return {"reinforced": 0, "promoted": 0}
    reinforced = 0
    promoted = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        data = load()
        learned = [e for entries in data["learned_defects"].values()
                   for e in entries]
        # 命中强化
        for e in learned:
            term = e.get("term", "")
            if term and any(term in t for t in titles):
                e["hits"] = int(e.get("hits", 0)) + 1
                e["last_seen"] = now
                reinforced += 1

        if defect_context:
            candidates = data["candidates"]
            for title in titles:
                for m in _MINE_PATTERN.finditer(title):
                    phrase = m.group(0)
                    if not (_MINED_MIN_LEN <= len(phrase) <= _MINED_MAX_LEN):
                        continue
                    if phrase in known_forms:
                        continue
                    # 复合 query 不学习（「摔坏的手机」学进去会吞设备位，
                    # 展开出「摔坏的手机的手机」）；修饰性新词形放行
                    if _is_composite_query(phrase, known_forms):
                        continue
                    c = candidates.get(phrase)
                    if c is None:
                        if len(candidates) >= _MAX_CANDIDATES:
                            continue
                        candidates[phrase] = {"count": 1, "last_seen": now}
                    else:
                        c["count"] += 1
                        c["last_seen"] = now
            # 转正
            for phrase in [p for p, c in candidates.items()
                           if c["count"] >= _PROMOTE_HITS]:
                fam = infer_family(phrase)
                data["learned_defects"].setdefault(fam, []).append({
                    "term": phrase, "source": "mined",
                    "hits": candidates[phrase]["count"],
                    "first_seen": now, "last_seen": now})
                known_forms.add(phrase)
                del candidates[phrase]
                promoted += 1

        _save(data)
    return {"reinforced": reinforced, "promoted": promoted}


# ---------------------------------------------------------------------------
# AI 展开结果缓存（按关键词）
# ---------------------------------------------------------------------------

def get_ai_variants(keyword: str) -> Optional[List[str]]:
    """返回缓存的 AI 变体；None=未缓存（需要调 AI），[]=AI 判非瑕疵
    （负缓存，7 天内不再问）。"""
    entry = (load().get("ai_cache") or {}).get(keyword)
    if not entry:
        return None
    variants = entry.get("variants") or []
    if not variants and time.time() - entry.get("ts", 0) > _AI_NEGATIVE_TTL:
        return None  # 负缓存过期，允许重试
    return variants


def set_ai_variants(keyword: str, variants: List[str]) -> None:
    with _LOCK:
        data = load()
        cache = data["ai_cache"]
        if len(cache) >= 1000:  # 上限防爆
            oldest = sorted(cache, key=lambda k: cache[k].get("ts", 0))[:200]
            for k in oldest:
                cache.pop(k, None)
        cache[keyword] = {"variants": variants, "ts": time.time()}
        _save(data)
