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
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 词族表（组内互为近义/同义；族的排列顺序 = 跨族展开优先级）
# ---------------------------------------------------------------------------

# 瑕疵定义词族：每族第一个词是该族的「代表词」，跨族展开时优先用代表词。
# 除书面词外收录口语/事件型表述（卖家实际写法：淋雨了/可乐倒了/车压了），
# 靠这些词去搜才能捞到对应描述的商品。
DEFECT_FAMILIES: List[Tuple[str, List[str]]] = [
    ("摔坏", ["摔坏", "摔碎", "磕坏", "摔了", "磕碰", "磕了", "掉地上", "掉地"]),
    ("屏幕破", ["屏幕破", "屏幕碎", "碎屏", "爆屏", "屏碎", "屏破", "裂屏", "屏裂",
                "屏幕裂", "外屏碎", "内屏坏"]),
    ("坏", ["坏的", "坏了", "故障", "不开机", "点不亮", "坏板", "不亮", "死机",
            "黑屏", "开不了机", "无法开机"]),
    ("变形", ["变形", "弯曲", "弯了", "压坏", "压扁", "车压了", "压了", "坐弯",
              "坐坏", "挤坏"]),
    ("进水", ["进水", "泡水", "淋雨", "受潮", "掉水里", "水漏了", "漏水", "渗漏",
              "洒饮料", "可乐倒了", "洒了", "溅水", "打湿"]),
    # 屏幕显示瑕疵（二手验机高频表述：黑点/白点/条纹/划痕/印记）
    ("屏幕瑕疵", ["屏幕有黑点", "黑点", "白点", "亮点", "坏点", "亮斑", "条纹",
                "花屏", "烧屏", "透图", "划痕", "印记", "泛黄"]),
    # 部件级功能失灵（笔记本键盘/触摸板、充电、电池等）
    ("部件失灵", ["键盘不能用", "键盘失灵", "触摸不灵", "触摸失灵", "触控不灵",
                "触控失灵", "掉键", "掉了几个键", "按键失灵", "充不进电",
                "不充电", "电池鼓包", "风扇异响", "接口松动"]),
    # 充气类商品专属（桨板/充气艇/气球等）
    ("漏气", ["跑气", "漏气", "慢撒气", "气压不足", "打不进气"]),
    # 被修补过（二手描述里的隐性瑕疵：补了一块/打过补丁）
    ("修补", ["补了一块", "打过补丁", "修补过", "补过", "粘过", "焊过"]),
]

# 弱瑕疵词：单独出现在关键词里语义歧义大（「条纹衬衫」「护手霜」不含设备词时
# 不应触发裸词配对）。只有关键词里同时出现设备词时才按瑕疵处理。
WEAK_DEFECT_FORMS = {
    "划痕", "条纹", "印记", "泛黄", "压了", "洒了", "弯了", "补过", "粘过",
    "异响", "死机", "不亮",
}

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
    ("桨板", ["桨板", "充气桨板", "冲浪板", "皮划艇", "充气艇", "橡皮艇"]),
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


def max_variants() -> int:
    """公开版：当前生效的变体上限（spider 拼 AI 变体时用）。"""
    return _max_variants()


# ---------------------------------------------------------------------------
# 学习词库合并（内置词库打底 + keyword_lexicon_store 里 AI/挖到的新词）
# ---------------------------------------------------------------------------

def _merge_learned(builtin: List[Tuple[str, List[str]]],
                   learned: List[Tuple[str, List[str]]]
                   ) -> List[Tuple[str, List[str]]]:
    merged = [(canon, list(forms)) for canon, forms in builtin]
    index = {canon: i for i, (canon, _f) in enumerate(merged)}
    for fam, terms in learned:
        if fam in index:
            i = index[fam]
            merged[i][1].extend(t for t in terms if t not in merged[i][1])
        else:
            merged.append((fam, list(terms)))
    return merged


def _effective_defect_families() -> List[Tuple[str, List[str]]]:
    try:
        from common.keyword_lexicon_store import learned_defect_families
        return _merge_learned(DEFECT_FAMILIES, learned_defect_families())
    except Exception:  # 学习库不可用不挡路，退回内置词库
        return DEFECT_FAMILIES


def _effective_device_families() -> List[Tuple[str, List[str]]]:
    try:
        from common.keyword_lexicon_store import learned_device_families
        return _merge_learned(DEVICE_FAMILIES, learned_device_families())
    except Exception:
        return DEVICE_FAMILIES


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


def _defect_variants(families: List[Tuple[str, List[str]]],
                     family_idx: int, matched_form: str) -> List[str]:
    """瑕疵词变体序列：原词 → 其他族代表词（按词族表序）→ 本族其余近义词。"""
    canon, forms = families[family_idx]
    seq: List[str] = [matched_form]
    for i, (other_canon, other_forms) in enumerate(families):
        if i != family_idx and other_forms:
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
    _canon, forms = _effective_device_families()[family_idx]
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
        # 「的」去重：替换词以「的」结尾且原文紧跟「的」（如「掉地上的耳机」
        # 中「掉地上」→「坏的」），吃掉原文那个「的」，避免「坏的的耳机」
        if repl.endswith("的") and out[end:end + 1] == "的":
            end += 1
        out = out[:start] + repl + out[end:]
    return out


def classify_keyword(keyword: str) -> Dict[str, Optional[str]]:
    """分类关键词：返回 {'defect': 命中的瑕疵词形|None, 'device': 命中的设备词形|None}。

    spider 据此决定是否调 AI 兜底（defect 为 None 时）以及本轮是否为
    瑕疵语境（挖掘候选词的开关）。弱瑕疵词在无设备词时不算命中。
    """
    text = _normalize(keyword)
    if not text:
        return {"defect": None, "device": None}
    defect = _find_span(text, _effective_defect_families())
    device = _find_span(text, _effective_device_families())
    if defect and device and not (device[2] <= defect[1] or defect[2] <= device[1]):
        device = None
    if defect and defect[3] in WEAK_DEFECT_FORMS and device is None:
        defect = None
    return {"defect": defect[3] if defect else None,
            "device": device[3] if device else None}


def extract_residual(variant: str) -> str:
    """从（AI 给出的）变体 query 中剥离设备词与的/了/空格胶，得到残余的
    瑕疵表述——用于把 AI 变体反解成可入库的瑕疵单词。"""
    text = _normalize(variant)
    device = _find_span(text, _effective_device_families())
    if device:
        text = (text[:device[1]] + text[device[2]:]).strip()
    return text.strip("的了 ")


def expand_keyword(keyword: str, max_variants: Optional[int] = None) -> List[str]:
    """把含瑕疵定义词的关键词展开为多个搜索词（原词优先，去重，限量）。

    不触发展开的情形原样返回 [keyword]：
    - 关键词不含任何瑕疵定义词（无论有没有设备词）
    - 仅命中弱瑕疵词且关键词里没有设备词（「条纹衬衫」类歧义）
    - KEYWORD_EXPAND_ENABLED=false
    """
    raw = (keyword or "").strip()
    if not raw:
        return []
    if not _expand_enabled():
        return [raw]
    limit = max_variants or _max_variants()

    text = _normalize(raw)
    defect_families = _effective_defect_families()
    device_families = _effective_device_families()
    defect = _find_span(text, defect_families)
    device = _find_span(text, device_families)

    # 瑕疵词与设备词 span 重叠时（理论上罕见），以瑕疵词为准放弃设备展开
    if defect and device and not (device[2] <= defect[1] or defect[2] <= device[1]):
        device = None

    # 弱瑕疵词单独出现歧义大（「条纹衬衫」），无设备词时不展开
    if defect and device is None and defect[3] in WEAK_DEFECT_FORMS:
        return [raw]

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

    d_variants = _defect_variants(defect_families, defect[0], defect[3])
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
