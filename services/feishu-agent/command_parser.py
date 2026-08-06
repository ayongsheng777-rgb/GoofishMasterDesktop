# -*- coding: utf-8 -*-
"""Command Parser — Natural language to structured commands for the V2.0 agent system."""
from __future__ import annotations
import os, re, json, logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

AI_ROUTER_URL = os.environ.get("AI_ROUTER_URL", "http://ai-router:8902")


def parse_command(text: str) -> Dict[str, Any]:
    """Parse natural language command into structured format.

    Examples:
        "找一台二手Mac Studio 要求个人卖家 价格7000以下"
        → {"action": "search", "keyword": "Mac Studio", "seller_type": "personal", "max_price": 7000}

        "监控 RTX4090 低于8000"
        → {"action": "monitor", "keyword": "RTX4090", "max_price": 8000}
    """
    text = text.strip()
    if not text:
        return {"action": "help"}

    # Help command (with topic support)
    help_topic_match = re.match(r'帮助\s*(搜索|监控|分析|风险|卖家|价格|设置)', text)
    if help_topic_match:
        return {"action": "help", "topic": help_topic_match.group(1)}

    if text in ("帮助", "help", "?", "？", "使用说明"):
        return {"action": "help"}

    # Status command
    if text in ("状态", "status", "运行状态"):
        return {"action": "status"}

    # 闲鱼扫码登录（须在前缀指令与裸文本兜底之前：否则「闲鱼登录」会被当成搜索词）
    if re.match(r'(闲鱼\s*(扫码)?\s*(登录|登陆))|((登录|登陆)\s*闲鱼)', text):
        return {"action": "xianyu_login"}

    # Last search results list（须在 search 前缀匹配之前：
    # 「搜索结果」会被「搜索」前缀吃掉成 keyword=「结果」）
    if text in ("结果列表", "全部结果", "所有结果", "上次结果", "搜索结果", "结果"):
        return {"action": "list_results"}

    # Search command（「收」是二手场景高频动词；负向预查挡住 收到/收好/收了 等闲聊）
    search_match = re.match(r'(?:找|搜索|查找|寻找|搜|收(?!到|好|了|下|藏|尾|工|音|费|税))\s*(.+?)(?:\s+要求|\s+条件|$)', text)
    if search_match:
        keyword = search_match.group(1).strip()
        result = {"action": "search", "keyword": keyword}
        _parse_conditions(text, result)
        _extract_exclusions(text, result)
        result["keyword"] = _clean_keyword(keyword, result)
        return result

    # Monitor command
    monitor_match = re.match(r'(?:监控|关注|盯着|订阅)\s*(.+?)(?:\s+低于|\s+要求|$)', text)
    if monitor_match:
        keyword = monitor_match.group(1).strip()
        result = {"action": "monitor", "keyword": keyword}
        _parse_conditions(text, result)
        _extract_exclusions(text, result)
        result["keyword"] = _clean_keyword(keyword, result)
        return result

    # Analyze command
    analyze_match = re.match(r'(?:分析|看看|检查|鉴定)\s*(?:第(\d+)个)?\s*(.*)', text)
    if analyze_match:
        index = analyze_match.group(1)
        keyword = analyze_match.group(2).strip()
        result = {"action": "analyze"}
        if index:
            result["index"] = int(index)
        if keyword:
            result["keyword"] = keyword
        return result

    # Blacklist command
    if re.match(r'(?:拉黑|屏蔽|黑名单)\s*(.+)', text):
        target = re.match(r'(?:拉黑|屏蔽|黑名单)\s*(.+)', text).group(1).strip()
        return {"action": "blacklist", "target": target}

    # List command
    if text in ("任务列表", "我的任务", "列表", "查看任务"):
        return {"action": "list_tasks"}

    # Delete command — 必须放在停止之前且独立动作：历史上「删除」被
    # (?:停止|取消|删除) 合并正则吞进 stop_task，任务永远删不掉（2026-08-03 实锤）。
    del_match = re.match(r'删除\s*(?:任务|监控)?\s*(.+)', text)
    if del_match:
        target = del_match.group(1).strip()
        return {"action": "delete_task", "target": target}

    # Stop search command — 必须先于通用「停止」正则：否则「停止搜索」会被
    # stop_match 吞成 target="搜索" 去停监控任务，实际什么都停不掉。
    if re.fullmatch(r'(?:停止|取消)\s*搜索(?:任务)?', text):
        return {"action": "stop_search"}

    # Stop command
    stop_match = re.match(r'(?:停止|取消)\s*(?:任务|监控)?\s*(.+)', text)
    if stop_match:
        target = stop_match.group(1).strip()
        return {"action": "stop_task", "target": target}

    # Task settings command: 设置 4090 间隔60分钟 / 设置 4090 阈值70
    set_match = re.match(r'设置\s*(.+?)\s*(间隔|阈值)\s*(\d+)\s*(?:分钟|分)?\s*$', text)
    if set_match:
        result = {"action": "set_task", "target": set_match.group(1).strip()}
        if set_match.group(2) == "间隔":
            result["interval_minutes"] = int(set_match.group(3))
        else:
            result["min_score"] = int(set_match.group(3))
        return result

    # Default: bare text as search — with an intent gate to prevent casual
    # chatter from triggering a full scrape + dozens of AI calls (cost hole).
    # 规则：过短(<3字符)、常见闲聊、纯标点 → 按闲聊处理，不触发搜索。
    if len(text) > 1:
        if _looks_like_chatter(text):
            return {"action": "chatter", "text": text}
        result = {"action": "search", "keyword": text}
        _parse_conditions(text, result)
        _extract_exclusions(text, result)
        result["keyword"] = _clean_keyword(text, result)
        return result

    return {"action": "help"}


_CHATTER_PHRASES = {
    "好", "好的", "好吧", "好哒", "嗯", "嗯嗯", "恩", "哦", "哦哦", "噢",
    "谢谢", "谢了", "多谢", "感谢", "ok", "okay", "o", "kk",
    "收到", "了解", "明白", "知道了", "懂了", "可以", "行", "能行",
    "是的", "对", "没错", "是的呢", "没事", "没关系", "辛苦了",
    "在吗", "在", "在不在", "人呢", "哈哈", "哈哈哈", "呵呵", "嘿嘿",
    "早", "早上好", "晚安", "你好", "您好", "hi", "hello", "喂",
}


def _looks_like_chatter(text: str) -> bool:
    """Heuristic intent gate: True if the text is casual chatter, not a search."""
    t = text.strip().lower().rstrip("。.!！?？~～")
    if not t:
        return True
    if t in _CHATTER_PHRASES:
        return True
    # 短于 3 个字符且不含数字/字母的商品词（如"在吗"类）按闲聊处理；
    # 纯中文且长度>=3 的商品名（如"Mac mini"无歧义但"今天天气"也有歧义）
    # —— 折衷：无搜索前缀时，纯中文短句(<4字)不触发，需要用户说「找 XX」
    if len(text) < 3:
        return True
    has_alnum = any(c.isalnum() and ord(c) < 128 for c in text)
    if not has_alnum and len(text) < 4:
        return True
    return False


def _parse_conditions(text: str, result: Dict[str, Any]) -> None:
    """Extract price, seller type, and other conditions from text."""
    # Price conditions
    price_match = re.search(r'(?:价格|预算)?\s*(\d+)\s*(?:元|块|k|K)?\s*(?:以下|以内|之内|不超过)', text)
    if price_match:
        price = int(price_match.group(1))
        if 'k' in text.lower() or 'K' in text:
            price *= 1000
        result["max_price"] = price

    price_match2 = re.search(r'(?:低于|不超过|不高于)\s*(\d+)', text)
    if price_match2:
        result["max_price"] = int(price_match2.group(1))

    price_range = re.search(r'(\d+)\s*[-~到]\s*(\d+)', text)
    if price_range:
        result["min_price"] = int(price_range.group(1))
        result["max_price"] = int(price_range.group(2))

    # Seller type
    if re.search(r'个人\s*(?:卖家|卖|闲置)', text):
        result["seller_type"] = "personal"
    elif re.search(r'商家|店铺|专营', text):
        result["seller_type"] = "business"

    # Condition/quality
    if re.search(r'全新|未拆', text):
        result["condition"] = "new"
    elif re.search(r'二手|用过', text):
        result["condition"] = "used"

    # Risk keywords
    if re.search(r'无矿卡|不要矿卡|非矿', text):
        result["exclude_mining"] = True
    if re.search(r'无维修|没修过', text):
        result["exclude_repair"] = True


# 排除词并列连词：「不要主机或其它配件」「排除矿卡和维修机」需拆开，
# 否则整词「主机或其它配件」子串匹配永不命中（实测 2026-08-02）。
_EXCLUDE_CONNECTIVES = re.compile(r'以及|还有|或者|或|和|与|、|，')


def _split_exclude_word(word: str) -> list:
    """Split conjoined exclude word and strip filler prefixes.

    「主机或其它配件」→ ["主机", "配件"]；「其它」类前缀是语气填充，
    留着会让子串匹配再次失效（标题写「配件」不写「其它配件」）。
    """
    parts = []
    for p in _EXCLUDE_CONNECTIVES.split(word):
        p = re.sub(r'^(?:其它|其他|别的|相关)+', '', p).strip(' 的')
        if p and p not in ("矿卡",) and not re.match(r'^\d+$', p):
            parts.append(p)
    return parts


def _extract_exclusions(text: str, result: Dict[str, Any]) -> None:
    """Extract exclude keywords: 不要搜配件 / 不要配件 / 排除配件 → ["配件"].

    「不要矿卡」已由 exclude_mining 标志覆盖，不重复进排除词表。
    """
    excludes: list = []
    for m in re.finditer(r'不要\s*(?:搜|看|要)?\s*([一-龥A-Za-z0-9]{1,10}?)(?=\s|$|，|。|、)', text):
        excludes.extend(_split_exclude_word(m.group(1)))
    for m in re.finditer(r'排除\s*([一-龥A-Za-z0-9]{1,10}?)(?=\s|$|，|。|、)', text):
        excludes.extend(_split_exclude_word(m.group(1)))
    if excludes:
        # 去重保持顺序
        seen = set()
        result["exclude_keywords"] = [w for w in excludes if not (w in seen or seen.add(w))]


# 需要从关键词中剥离的条件短语（价格/卖家类型/成色/风险标志等）。
# 这些片段的信息已被 _parse_conditions 结构化，留在关键词里只会污染闲鱼搜索。
_CONDITION_PATTERNS = [
    r'(?:价格|预算)\s*\d+\s*(?:元|块|k|K)?\s*(?:以下|以内|之内|不超过)?',
    r'(?:低于|不超过|不高于)\s*\d+\s*(?:元|块|k|K)?',
    r'\d+\s*(?:元|块|k|K)?\s*(?:以下|以内|之内)',
    r'\d+\s*[-~到]\s*\d+\s*(?:元|块)?',
    r'不要\s*(?:搜|看|要)?\s*[一-龥A-Za-z0-9]{1,10}(?=\s|$|，|。|、)',
    r'排除\s*[一-龥A-Za-z0-9]{1,10}(?=\s|$|，|。|、)',
    # 「只要/仅要 X」：限定意图，X 通常已在关键词主体中（「iphone15手机 只要手机」），
    # 残留会污染闲鱼搜索词；X 可选（「只要9000以下」的价格先被价格模式剥掉后「只要」落单）；
    # 剥光时 _clean_keyword 回退原文兜底
    r'(?:只|仅)\s*要(?:\s*[一-龥A-Za-z0-9]{1,10})?(?=\s|$|，|。|、)',
    # 指令动词残留（裸文本/倒装语序：「排除翻新机 找 iphone15」剥排除后「找」才到开头，
    # 故放列表末尾且允许前导空格）
    r'^\s*(?:找|搜索|查找|寻找|搜|收)\s+',
    r'个人\s*(?:卖家|卖|闲置)', r'商家|店铺|专营',
    r'全新|未拆', r'二手|用过',
    r'无矿卡|不要矿卡|非矿', r'无维修|没修过',
]


def _clean_keyword(keyword: str, result: Dict[str, Any]) -> str:
    """Strip structured condition phrases from the search keyword.

    「搜 iphone15手机 不要搜配件 低于1800元」应搜 "iphone15手机"，
    而不是把整句话丢给闲鱼（实测返回 0 件）；剥不干净时回退原文。
    """
    cleaned = keyword
    for pat in _CONDITION_PATTERNS:
        cleaned = re.sub(pat, ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ,，。')
    if not cleaned:
        logger.warning("关键词清洗后为空，回退原文: %s", keyword)
        return keyword
    if cleaned != keyword:
        logger.info("关键词清洗: '%s' → '%s'", keyword, cleaned)
    return cleaned


async def ai_refine_command(text: str, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """AI 理解式指令提炼（search/monitor 专用，正则结果兜底）。

    正则 parser 是「减法」——只能剥离已知模式；自然语言变体无限
    （「要没有升级存储的」「带原箱说全的」），剥离不干净时整句
    残留进闲鱼搜索词导致 0 结果（实测 oesp 任务 2026-08-02）。
    交给模型理解任务主题后提炼，任何失败静默回退正则结果不变。
    """
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(f"{AI_ROUTER_URL}/api/chat", json={
                "task": "command_parse", "content": text, "max_tokens": 500})
        data = resp.json()
        if not data.get("success"):
            logger.warning("AI 指令提炼返回失败: %s", data.get("error", ""))
            return cmd
        parsed = data.get("parsed") or {}

        # 关键词：非空、长度合理、与原文不同才覆盖（防幻觉扩张）
        kw = str(parsed.get("keyword") or "").strip(" ,，。")
        if kw and len(kw) <= 30 and kw != text:
            if kw != cmd.get("keyword"):
                logger.info("AI 提炼关键词: '%s' → '%s'（正则 '%s' 被覆盖）",
                            text, kw, cmd.get("keyword"))
                cmd["keyword"] = kw

        # 排除词：AI 与正则结果合并去重（AI 理解反向语义，正则抓显式「不要」）
        ai_ex = [str(w).strip() for w in (parsed.get("exclude_keywords") or [])
                 if str(w).strip()]
        if ai_ex:
            merged = list(cmd.get("exclude_keywords") or [])
            for w in ai_ex:
                if w not in merged:
                    merged.append(w)
            if merged != cmd.get("exclude_keywords"):
                logger.info("AI 提炼排除词: %s（合并后）", merged)
            cmd["exclude_keywords"] = merged

        # 价格/卖家类型：正则已提取的优先（数字精确），缺省才用 AI 的
        if not cmd.get("max_price") and parsed.get("max_price"):
            try:
                v = int(parsed["max_price"])
                if v > 0:
                    cmd["max_price"] = v
            except (TypeError, ValueError):
                pass
        if not cmd.get("seller_type") and parsed.get("seller_type") in ("personal", "business"):
            cmd["seller_type"] = parsed["seller_type"]
    except Exception as e:
        logger.warning("AI 指令提炼失败，回退正则结果: %s", e)
    return cmd


def format_help() -> str:
    return """🎣 GoofishMasterDesktop 使用指南

📝 基本指令：
• 找/搜/收 [商品名] — 搜索商品（自然语言即可，AI 自动提炼关键词）
• 监控 [商品名] — 持续监控
• 分析 [商品名] — 深度分析
• 分析第N个 — 分析上次搜索结果
• 结果列表 — 上次搜索的全部商品（标题+链接）

⚙️ 条件筛选：
• 价格XXXX以下 / 低于XXXX / XXXX-XXXX区间
• 个人卖家 / 商家
• 全新 / 二手
• 无矿卡 / 无维修 / 不要X（配件/刷机/主机…，AI 自动扩展同义词）

📋 管理指令：
• 任务列表 — 查看所有任务
• 停止搜索 — 停止正在进行的一次性搜索
• 停止 [任务名] — 停止监控
• 删除 [任务名] — 删除监控任务（含去重记录）
• 设置 [任务名] 间隔XX分钟 — 修改监控间隔
• 设置 [任务名] 阈值XX — 修改推送分数
• 拉黑 [卖家名] — 加入黑名单
• 闲鱼登录 — 推送闲鱼扫码二维码到飞书，扫码即登录
• 状态 — 查看系统状态

❓ 专题帮助：
• 帮助搜索 — 搜索功能详解
• 帮助监控 — 监控任务详解
• 帮助分析 — AI 分析详解
• 帮助风险 — 风险识别详解
• 帮助卖家 — 卖家识别详解
• 帮助价格 — 价格评估详解
• 帮助设置 — 系统设置详解

💡 示例：
• 找 RTX4090 个人卖家 价格8000以下
• 收 b460主板 只要主板，不要主机或配件
• 监控 Mac mini M4 低于5000
• 结果列表"""


HELP_TOPICS = {
    "搜索": """🔍 搜索功能详解

基本用法：
• 找 [商品名] — 搜索商品
• 找 [商品名] 价格XXXX以下 — 限定价格
• 找 [商品名] 个人卖家 — 只看个人卖家

高级组合：
• 找 RTX4090 个人卖家 价格8000以下 无矿卡
• 找 MacBook Pro 全新 价格5000-8000

搜索流程：
1. AI 提炼搜索关键词与排除词（理解反向语义，如「不要升级存储」→ 排除对应词）
2. Spider 采集闲鱼商品
3. AI 多维度分析（卖家/风险/价格）
4. 按综合评分排序推送结果

提示：
• 关键词越具体，结果越精准
• 可以组合多个条件缩小范围
• 搜索结果是实时的，可能需要几分钟""",

    "监控": """📡 监控任务详解

创建监控：
• 监控 [商品名] — 基础监控
• 监控 [商品名] 低于XXXX — 价格触发
• 监控 [商品名] 个人卖家 低于XXXX — 组合条件

管理任务：
• 任务列表 — 查看所有监控
• 停止 [任务名] — 停止指定监控
• 删除 [任务名] — 删除监控任务
• 设置 [任务名] 间隔XX分钟 — 修改监控间隔
• 设置 [任务名] 阈值XX — 修改推送分数门槛

监控机制：
• 每 30 分钟自动搜索一次
• 发现新商品自动 AI 分析
• 符合条件（评分≥60）自动推送通知
• 重复商品自动去重

示例：
• 监控 Mac mini M4 低于5000
• 监控 RTX4090 个人卖家 低于8000""",

    "分析": """🔬 AI 分析详解

分析类型：
• 分析 [商品名] — 深度分析商品
• 分析第N个 — 分析上次搜索的第N个结果

分析维度：
1. 🧑 卖家身份识别
   - 个人卖家 vs 专业商家
   - 账号行为分析
   - 商品特征匹配
   - 可信度评分 (0-100)

2. ⚠️ 风险检测
   - 虚假描述识别
   - 图片盗用检测
   - 价格异常分析
   - 诈骗套路识别
   - 风险评分 (0-100)

3. 💰 价格评估
   - 市场价估算
   - 历史成交价对比
   - 当前折扣计算
   - 性价比评分

4. 🎯 综合决策
   - 风险安全 30%
   - 价格优势 30%
   - 卖家可信 25%
   - 需求匹配 15%
   - 最终评分 (0-100)

评分等级：
• A级 (90-100): 强烈推荐，立即查看
• B级 (75-90): 建议关注
• C级 (60-75): 普通
• D级 (<60): 不建议""",

    "风险": """⚠️ 风险识别详解

检测项目：
1. 假货风险
   - 描述与图片不一致
   - 品牌冒充
   - 参数造假

2. 翻新风险
   - 包装磨损痕迹
   - 螺丝拆装痕迹
   - PCB 使用痕迹
   - 防拆标签状态

3. 矿卡风险
   - 价格异常低
   - 大量同型号
   - 卖家行为特征
   - 显卡使用痕迹

4. 诈骗风险
   - 不退不换条款
   - 仅展示不发货
   - 图片仅供参考
   - 渠道货/工程机

5. 隐藏缺陷
   - 描述中规避质量责任
   - 模糊表述
   - 避重就轻

风险关键词：
• 高风险: 不退不换、仅展示、工程机、渠道货
• 中风险: 小问题、偶尔重启、自行检测、当配件卖

风险等级：
• 🟢 低 (<30分): 可以放心购买
• 🟡 中 (30-60分): 需要谨慎确认
• 🔴 高 (>60分): 不建议购买""",

    "卖家": """👤 卖家识别详解

识别维度：
1. 账号行为 (20%)
   - 注册时间
   - 活跃度
   - 交易频率

2. 商品特征 (20%)
   - 商品数量
   - 同类商品比例
   - 品牌集中度

3. 发布规律 (15%)
   - 发布时间规律性
   - 批量发布特征

4. 描述模式 (15%)
   - 文案重复度
   - 模板化程度
   - 语言自然度

5. 图片特征 (15%)
   - 图片重复率
   - 拍摄风格一致性
   - 生活化程度

6. 价格行为 (15%)
   - 定价规律性
   - 价格竞争特征

卖家类型：
• 个人卖家 ✅
  特征：商品少、图片生活化、描述自然、价格随意
  可信度：通常 80-95 分

• 专业商家 ⚠️
  特征：大量库存、统一模板、批量图片、定价规律
  可信度：通常 40-60 分

• 可疑商家 ❌
  特征：伪装个人、新注册、信息不全
  可信度：通常 <40 分""",

    "价格": """💰 价格评估详解

评估方法：
1. 市场价估算
   - 基于同类商品近期成交价
   - 考虑商品成色/配置差异
   - 区域价格差异修正

2. 折扣计算
   - 当前价格 vs 市场价
   - 折扣率 = (市场价 - 当前价) / 市场价

3. 性价比评分
   - 折扣 >30%: 优秀 (90-100分)
   - 折扣 20-30%: 良好 (75-90分)
   - 折扣 10-20%: 一般 (60-75分)
   - 折扣 <10%: 偏高 (<60分)

价格异常检测：
• 低于市场价 40%+ → 高风险警告
• 可能是: 假货/翻新/矿卡/诈骗
• 触发深度 AI 分析

示例：
• 市场价 ¥8500，售价 ¥6800 → 折扣 20%，良好
• 市场价 ¥8500，售价 ¥4999 → 折扣 41%，高风险！""",

    "设置": """⚙️ 系统设置详解

管理后台：
• 地址: http://localhost:8901
• 首次使用需要设置 OTP 验证器

功能模块：
1. 📱 飞书配置
   - 扫码绑定飞书机器人
   - 查看机器人运行状态
   - 发送测试消息

2. 🧠 AI 模型配置
   - 支持 6 个 AI 提供商
   - OpenAI / DeepSeek / 通义千问 / Gemini / 智谱 / Kimi
   - 每个提供商独立配置 API Key
   - 支持模型列表拉取和选择
   - 任务路由规则配置

3. 🐟 闲鱼登录
   - 扫码登录闲鱼
   - 上传 Chrome 扩展导出的登录状态
   - 管理登录状态文件

4. 📊 系统概览
   - 查看所有服务运行状态
   - AI 调用统计
   - 分析结果统计

Chrome 扩展：
• 闲鱼登录状态导出工具
• 安装后一键导出 xianyu_state.json
• 在管理后台上传即可完成登录""",
}


def format_help_topic(topic: str) -> str:
    """Get help for a specific topic."""
    return HELP_TOPICS.get(topic, format_help())


def format_help_card() -> dict:
    """Return help as a Feishu interactive card."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "🎣 GoofishMasterDesktop 使用指南"}
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "**📝 基本指令**\n"
                "• 找/搜/收 [商品名] — 搜索（AI 自动提炼关键词）\n"
                "• 监控 [商品名] — 持续监控\n"
                "• 分析 [商品名] — 深度分析\n"
                "• 分析第N个 — 分析上次搜索结果\n"
                "• 结果列表 — 上次搜索的全部商品（标题+链接）"
            }},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "**⚙️ 条件筛选**\n"
                "• 价格XXXX以下 / 低于XXXX / XXXX-XXXX区间\n"
                "• 个人卖家 / 商家\n"
                "• 全新 / 二手\n"
                "• 无矿卡 / 无维修 / 不要X（配件/刷机/主机…）"
            }},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "**📋 管理指令**\n"
                "• 任务列表 — 查看所有任务\n"
                "• 停止搜索 — 停止正在进行的一次性搜索\n"
                "• 停止 [任务名] — 停止监控\n"
                "• 删除 [任务名] — 删除监控任务\n"
                "• 设置 [任务名] 间隔XX分钟 / 阈值XX — 修改任务参数\n"
                "• 拉黑 [卖家名] — 加入黑名单\n"
                "• 闲鱼登录 — 推送扫码二维码到飞书，扫码即登录\n"
                "• 状态 — 查看系统状态"
            }},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "**❓ 专题帮助**\n"
                "• 帮助搜索 — 搜索功能详解\n"
                "• 帮助监控 — 监控任务详解\n"
                "• 帮助分析 — AI 分析详解\n"
                "• 帮助风险 — 风险识别详解\n"
                "• 帮助卖家 — 卖家识别详解\n"
                "• 帮助价格 — 价格评估详解\n"
                "• 帮助设置 — 系统设置详解"
            }},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "GoofishMasterDesktop"}]}
        ]
    }
