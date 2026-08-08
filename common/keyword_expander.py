# -*- coding: utf-8 -*-
"""瑕疵定义词关键词展开（搜索/监控共用）。

背景：用户在搜索/监控中输入「坏的」「摔坏的手机」这类带瑕疵定语的
关键词时，单一字面 query 覆盖面太窄——闲鱼卖家写「摔碎的 iphone」
「屏幕破的手机」「坏的笔记本电脑」各不相同。本模块把瑕疵定语与设备
名词各自按近义词族展开，交叉生成变体 query：

- 「摔坏的手机」→ 摔坏的手机 / 摔坏的iphone / 屏幕碎的手机 / 坏的手机
- 裸瑕疵词「坏的」→ 坏的手机 / 坏的iphone / 坏的笔记本电脑 / 坏的平板
  （裸瑕疵词单独搜索全是噪音，直接替换为设备配对，不保留原词）
- 纯设备词或不含瑕疵词的关键词原样返回，不展开。

展开上限默认 4 个（每个变体采集约 8 分钟，词越多单轮越慢），
可用环境变量 KEYWORD_EXPAND_MAX 调整；KEYWORD_EXPAND_ENABLED=false
可整体关闭（所有调用方退回单词行为）。
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# 词族表（组内互为近义/同义；族的排列顺序 = 跨族展开优先级）
# ---------------------------------------------------------------------------

# 瑕疵定义词族：每族第一个词是该族的「代表词」，跨族展开时优先用代表词
DEFECT_FAMILIES: List[Tuple[str, List[str]]] = [
    ("摔坏", ["摔坏", "摔碎", "磕坏", "磕碰"]),
    ("屏幕破", ["屏幕破", "屏幕碎", "碎屏", "爆屏", "屏碎", "屏破"]),
    ("坏", ["坏的", "坏了", "故障", "不开机", "点不亮", "坏板"]),
    ("变形", ["变形", "弯曲", "弯了"]),
    ("进水", ["进水", "泡水"]),
]

# 设备名词族：每族第一个词是代表词；族内其余为同义/常见别名
DEVICE_FAMILIES: List[Tuple[str, List[str]]] = [
    ("手机", ["手机", "iphone", "苹果手机"]),
    ("笔记本", ["笔记本电脑", "笔记本", "macbook"]),
    ("平板", ["平板电脑", "平板", "ipad"]),
    ("显卡", ["显卡", "gpu"]),
    ("相机", ["相机", "单反", "微单"]),
    ("耳机", ["耳机", "airpods"]),
    ("手表", ["手表", "apple watch", "智能手表"]),
    ("游戏机", ["游戏机", "switch", "ps5"]),
    ("台式机", ["台式电脑", "台式机", "电脑主机"]),
    ("无人机", ["无人机", "大疆"]),
]

# 裸瑕疵词（关键词里没有设备名词）时默认配对的设备词
DEFAULT_DEVICES: List[str] = ["手机", "iphone", "笔记本电脑", "平板"]

# ---------------------------------------------------------------------------
# 开关与上限（spider 与 pipeline 各自读进程 env，默认值保持一致）
# ---------------------------------------------------------------------------

def _expand_enabled() -> bool:
    return os.environ.get("KEYWORD_EXPAND_ENABLED", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def _max_variants() -> int:
    try:
        return max(1, int(os.environ.get("KEYWORD_EXPAND_MAX", "4")))
    except ValueError:
        return 4


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

def _normalize(keyword: str) -> str:
    return " ".join((keyword or "").lower().split())


def _find_span(text: str, families: List[Tuple[str, List[str]]]
               ) -> Optional[Tuple[int, int, int, str]]:
    """在 text 中找词族表面形式的最长匹配。

    返回 (family_idx, start, end, matched_form)；无匹配返回 None。
    同长度时按词族顺序优先（表靠前 = 优先级高）。
    """
    best: Optional[Tuple[int, int, int, str]] = None
    for fam_idx, (_canon, forms) in enumerate(families):
        # 同族内长形式优先（「笔记本电脑」先于「笔记本」，「坏的」先于「坏」）
        for form in sorted(forms, key=len, reverse=True):
            start = text.find(form)
            if start < 0:
                continue
            cand = (fam_idx, start, start + len(form), form)
            if best is None or len(form) > len(best[3]):
                best = cand
                break  # 该族已取到最长形式，看下一族
    return best


def _defect_variants(family_idx: int, matched_form: str) -> List[str]:
    """瑕疵词变体序列：原词 → 其他族代表词（按词族表序）→ 本族其余近义词。"""
    canon, forms = DEFECT_FAMILIES[family_idx]
    seq: List[str] = [matched_form]
    for i, (other_canon, other_forms) in enumerate(DEFECT_FAMILIES):
        if i != family_idx:
            seq.append(other_forms[0])
    seq.extend(f for f in forms if f != matched_form)
    # 去重保序
    seen, out = set(), []
    for f in seq:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _device_variants(family_idx: int, matched_form: str) -> List[str]:
    """设备词变体序列：原词 → 本族同义词（不跨族——用户指明了设备类别）。"""
    _canon, forms = DEVICE_FAMILIES[family_idx]
    return [matched_form] + [f for f in forms if f != matched_form]


def _replace_spans(text: str, defect_span, device_span,
                   new_defect: str, new_device: str) -> str:
    """按 span 替换瑕疵词/设备词，保留原始拼接结构（的/空格/后缀）。"""
    spans = []
    if defect_span:
        spans.append((defect_span[1], defect_span[2], new_defect))
    if device_span:
        spans.append((device_span[1], device_span[2], new_device))
    spans.sort(key=lambda s: s[0], reverse=True)  # 从后往前替换，索引不失效
    out = text
    for start, end, repl in spans:
        out = out[:start] + repl + out[end:]
    return out


def expand_keyword(keyword: str, max_variants: Optional[int] = None) -> List[str]:
    """把含瑕疵定义词的关键词展开为多个搜索词（原词优先，去重，限量）。

    不触发展开的情形原样返回 [keyword]：
    - 关键词不含任何瑕疵定义词（无论有没有设备词）
    - KEYWORD_EXPAND_ENABLED=false
    """
    raw = (keyword or "").strip()
    if not raw:
        return []
    if not _expand_enabled():
        return [raw]
    limit = max_variants or _max_variants()

    text = _normalize(raw)
    defect = _find_span(text, DEFECT_FAMILIES)
    device = _find_span(text, DEVICE_FAMILIES)

    # 瑕疵词与设备词 span 重叠时（理论上罕见），以瑕疵词为准放弃设备展开
    if defect and device and not (device[2] <= defect[1] or defect[2] <= device[1]):
        device = None

    if defect is None:
        return [raw]  # 无瑕疵定语：不展开（含纯设备词）

    if device is None:
        # 裸瑕疵词：单独搜索全是噪音 → 直接配对默认设备，不保留原词。
        # 拼接时补「的」（「摔坏」→「摔坏的手机」，「坏的」→「坏的手机」）。
        glue = "" if text.endswith("的") else "的"
        out: List[str] = []
        for dev in DEFAULT_DEVICES:
            out.append(text + glue + dev)
            if len(out) >= limit:
                break
        return out

    d_variants = _defect_variants(defect[0], defect[3])
    v_variants = _device_variants(device[0], device[3])

    # 交叉展开：原词 → 交替取设备同义词（原瑕疵）与瑕疵近义词（原设备），
    # 最后补交叉组合。两轴均衡覆盖，限量截断。
    candidates: List[str] = []
    candidates.append(_replace_spans(text, defect, device, d_variants[0], v_variants[0]))
    depth = max(len(d_variants), len(v_variants))
    for i in range(1, depth):
        if i < len(v_variants):
            candidates.append(_replace_spans(text, defect, device, d_variants[0], v_variants[i]))
        if i < len(d_variants):
            candidates.append(_replace_spans(text, defect, device, d_variants[i], v_variants[0]))
    for d in d_variants[1:]:
        for v in v_variants[1:]:
            candidates.append(_replace_spans(text, defect, device, d, v))

    seen, out = set(), []
    for cand in candidates:
        norm = _normalize(cand)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(cand)
        if len(out) >= limit:
            break
    return out
