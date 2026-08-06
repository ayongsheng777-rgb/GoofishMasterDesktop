# -*- coding: utf-8 -*-
"""AI Router — Multi-model dispatch service.

Routes AI tasks to appropriate models based on task type:
- image_analysis → Gemini Vision
- risk_analysis → GPT
- chinese_text → Qwen
- simple_filter → DeepSeek
- seller_analysis → GPT
- price_analysis → DeepSeek

Features:
- Hot config update via /api/config/update (persisted to DATA_DIR)
- Cross-model fallback chain when the chosen model fails/unconfigured
- OpenAI-compatible /v1/chat/completions passthrough (single entry for V1 engine)
- ai_logs persistence (Postgres, graceful)
- RAG knowledge injection for risk/product analysis (Qdrant, graceful)
"""
from __future__ import annotations
import asyncio, base64, os, json, logging, time
from pathlib import Path
from typing import Any, Dict, List, Optional
from enum import Enum

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import db
import rag

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# 日志脱敏：ai-router 是最容易泄漏 Key 的地方（上游报错常把 Authorization 头
# 原样回显）。详见 common/logfilter.py。
try:
    import sys as _sys
    from pathlib import Path as _P
    _r = str(_P(__file__).resolve().parents[2])
    if _r not in _sys.path:
        _sys.path.insert(0, _r)
    from common.logfilter import install as _install_logfilter
    _install_logfilter()
except Exception:
    pass

logger = logging.getLogger("ai-router")

app = FastAPI(title="GoofishMasterDesktop · AI Router", version="2.1.0")

# 兜底目录必须落在项目内（Docker 遗留的 "/app/data" 在 Windows 会写到盘符根）。
DATA_DIR = Path(os.environ.get("DATA_DIR")
                or Path(__file__).resolve().parents[2] / "data" / "ai-router")
DATA_DIR.mkdir(parents=True, exist_ok=True)
ROUTER_CONFIG_FILE = DATA_DIR / "router_config.json"

# 单模型槽位调用硬超时（秒）。必须 < pipeline 端 120s 客户端超时，
# 否则代理抖动时 gemini 多次重试会烧穿上游超时导致整件商品静默丢分。
SLOT_TIMEOUT = int(os.environ.get("AI_SLOT_TIMEOUT", "95"))


# ============ Model Configuration ============

class ModelProvider(str, Enum):
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    GEMINI = "gemini"
    CLAUDE = "claude"
    ZHIPU = "zhipu"
    MOONSHOT = "moonshot"


MODEL_CONFIG: Dict[str, Dict[str, Any]] = {
    "gpt": {
        "provider": ModelProvider.OPENAI,
        "model": os.environ.get("GPT_MODEL", "gpt-4o"),
        "api_key": os.environ.get("OPENAI_API_KEY", ""),
        "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "vision": True,   # gpt-4o 支持视觉
        "vision_model": os.environ.get("GPT_VL_MODEL", ""),  # 空 = 用主模型
        "use_proxy": False,
    },
    "deepseek": {
        "provider": ModelProvider.DEEPSEEK,
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        # 2026-08-03 实测：api.deepseek.com 仅 deepseek-v4-flash/pro 两个文本
        # 模型，均不收 image_url（400 unknown variant）——该平台无视觉能力
        "vision": False,
        "vision_model": os.environ.get("DEEPSEEK_VL_MODEL", ""),  # 未来出 VL 再配
        "use_proxy": False,
    },
    "qwen": {
        "provider": ModelProvider.QWEN,
        "model": os.environ.get("QWEN_MODEL", "qwen-max"),
        "api_key": os.environ.get("QWEN_API_KEY", ""),
        "base_url": os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        # 2026-08-03 实测：qwen-vl-max / qwen3-vl-plus / qwen-vl-plus 均可识别
        # 图片（http URL 与 data URI 双形态）；qwen-vl-max 最省 tokens；
        # 文本主模型（qwen-max 系）不收图，视觉走独立 vision_model
        "vision": True,
        "vision_model": os.environ.get("QWEN_VL_MODEL", "qwen-vl-max"),
        "use_proxy": False,
    },
    "gemini": {
        "provider": ModelProvider.GEMINI,
        "model": os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
        "api_key": os.environ.get("GEMINI_API_KEY", ""),
        "base_url": os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
        "vision": True,
        "vision_model": os.environ.get("GEMINI_VL_MODEL", ""),  # 空 = 用主模型
        "use_proxy": False,  # 国内网络需在 WebUI 开启「走代理」
        "ipv6": True,        # 代理 v4 出口被 Google 误判地区时，走应用层 IPv6 通道
    },
    "zhipu": {
        "provider": ModelProvider.ZHIPU,
        "model": os.environ.get("ZHIPU_MODEL", "glm-4-flash"),
        "api_key": os.environ.get("ZHIPU_API_KEY", ""),
        "base_url": os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        "vision": False,
        "vision_model": os.environ.get("ZHIPU_VL_MODEL", ""),
        "use_proxy": False,
    },
    "moonshot": {
        "provider": ModelProvider.MOONSHOT,
        "model": os.environ.get("MOONSHOT_MODEL", "moonshot-v1-8k"),
        "api_key": os.environ.get("MOONSHOT_API_KEY", ""),
        "base_url": os.environ.get("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1"),
        "vision": False,
        "vision_model": os.environ.get("MOONSHOT_VL_MODEL", ""),
        "use_proxy": False,
    },
}

# Task routing rules
# vision_analysis：含图消息的视觉调用首选槽位（WebUI 可设 "disabled" 不启用）。
# 旧键 image_analysis 在加载时迁移为本键。
TASK_ROUTING: Dict[str, str] = {
    "vision_analysis": "qwen",
    # 视频分析：商品含视频且启用时，先让视觉模型「看」视频出摘要注入分析。
    # 与 vision_analysis 独立设置；disabled = 不启用；首选失败（额度/网络/
    # 超时）即跳过，不降级不轮转（视频 ≈15k tokens/次，2026-08-03 实测）
    "video_analysis": "qwen",
    "risk_analysis": "gpt",
    "seller_analysis": "gpt",
    "price_analysis": "deepseek",
    "chinese_text": "qwen",
    "simple_filter": "deepseek",
    "search_keywords": "deepseek",
    "command_parse": "deepseek",
    "product_analysis": "gpt",
    "decision": "gpt",
}

# Fallback priority when the chosen model is unavailable/fails
FALLBACK_ORDER = ["deepseek", "gpt", "qwen", "zhipu", "moonshot", "gemini"]

# 输出必须是 JSON 的任务（prompt 里要求 JSON 格式输出）：
# 解析失败值得重试一次，且失败须打标供下游保守处理
_JSON_TASKS = {"product_analysis", "risk_analysis", "seller_analysis",
               "price_analysis", "search_keywords", "command_parse", "decision"}

# Vision chain for image messages: gemini 优先（视觉能力强/成本低），
# deepseek 兜底（已具备图像识别），其余按序。仅纳入已配 Key 且标记视觉的模型。
# 视觉链顺序（仅作历史参考/外部展示）：2026-08-03 起视觉调用改为
# 「路由表 vision_analysis 指定唯一首选」，失败即剥图降级文本（用户决策：
# 额度/网络问题直接跳过，不在视觉槽位间轮转烧钱）。
VISION_FALLBACK_ORDER = ["qwen", "gemini", "deepseek", "gpt", "zhipu", "moonshot"]

# Global proxy URL applied to providers with use_proxy=true（如国内访问 Gemini）。
# 环境变量 AI_PROXY_URL 或 WebUI 热配置（PROXY_URL）均可，后者优先。
PROXY_URL = os.environ.get("AI_PROXY_URL", "").strip()

# Provider-key → slot-name mapping (feishu-agent sends provider keys)
PROVIDER_TO_SLOT = {
    "openai": "gpt", "gpt": "gpt",
    "deepseek": "deepseek", "qwen": "qwen", "gemini": "gemini",
    "zhipu": "zhipu", "moonshot": "moonshot",
}


def _apply_routing_dict(routing: Dict[str, Any]) -> int:
    """Apply a task_routing dict to TASK_ROUTING.

    - 旧键 image_analysis 迁移为 vision_analysis（2026-08-03 视觉路由独立化）
    - "disabled" 是合法值（不启用），仅对 vision_analysis 生效；
      其他任务设 disabled 无意义，忽略。
    返回生效条数。
    """
    applied = 0
    for task, provider_key in routing.items():
        if task == "image_analysis":
            task = "vision_analysis"
        pk = str(provider_key).lower()
        if pk == "disabled":
            if task in ("vision_analysis", "video_analysis"):
                TASK_ROUTING[task] = "disabled"
                applied += 1
            continue
        slot = PROVIDER_TO_SLOT.get(pk)
        if slot:
            TASK_ROUTING[task] = slot
            applied += 1
    return applied

# Statistics
_stats = {"total_calls": 0, "calls_by_model": {}, "calls_by_task": {},
          "fallbacks": 0, "quota_fallbacks": 0}

# 命中这些特征的报错视为「无额度/限流」，触发轮转并单独统计
_QUOTA_MARKERS = ("insufficient", "quota", "balance", "余额", "额度", "429",
                  "402", "rate limit", "rate_limit", "resource_exhausted",
                  "exceeded")


def _is_quota_error(message: str) -> bool:
    m = (message or "").lower()
    return any(k in m for k in _QUOTA_MARKERS)


# 后台任务强引用集——event loop 对 task 只持弱引用，裸 create_task 会被 GC
# 中途回收（2026-08-03 监控轮次静默蒸发的根因）
_bg_tasks: set = set()


def _bg(coro) -> None:
    """Fire-and-forget with error surfacing + GC protection."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _bg_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("后台任务异常: %s", exc)

    task.add_done_callback(_on_done)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Robustly extract the first JSON object from model output.

    Strategy: (1) whole-text parse → (2) ```json fenced block →
    (3) balanced-brace scan from the first '{' (handles nesting, unlike
    the previous flat-regex approach which truncated nested objects).
    """
    import re
    if not text:
        return None
    # 1) whole text
    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # 2) fenced code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 3) balanced-brace scan
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


# ============ Persisted Config ============

def _save_router_config() -> None:
    try:
        payload = {
            "models": {slot: {"model": c["model"], "api_key": c["api_key"],
                              "base_url": c["base_url"],
                              "vision": c.get("vision", False),
                              "vision_model": c.get("vision_model", ""),
                              "use_proxy": c.get("use_proxy", False),
                              "ipv6": c.get("ipv6", False)}
                       for slot, c in MODEL_CONFIG.items()},
            "task_routing": TASK_ROUTING,
            "proxy_url": PROXY_URL,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        ROUTER_CONFIG_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to persist router config: %s", e)


def _load_persisted_config() -> None:
    """Overlay persisted config (from feishu-agent sync) onto env defaults."""
    global PROXY_URL
    if not ROUTER_CONFIG_FILE.exists():
        return
    try:
        payload = json.loads(ROUTER_CONFIG_FILE.read_text(encoding="utf-8"))
        for slot, conf in payload.get("models", {}).items():
            if slot in MODEL_CONFIG:
                for k in ("model", "api_key", "base_url", "vision_model"):
                    if conf.get(k):
                        MODEL_CONFIG[slot][k] = conf[k]
                for k in ("vision", "use_proxy", "ipv6"):
                    if k in conf:
                        MODEL_CONFIG[slot][k] = bool(conf[k])
        routing = payload.get("task_routing") or {}
        _apply_routing_dict(routing)
        if payload.get("proxy_url"):
            PROXY_URL = str(payload["proxy_url"]).strip()
        logger.info("Loaded persisted router config (%s)",
                    ROUTER_CONFIG_FILE.name)
    except Exception as e:
        logger.warning("Failed to load persisted router config: %s", e)


def _apply_env_style_update(payload: Dict[str, Any]) -> Dict[str, int]:
    """Apply env-style keys from feishu-agent sync_to_ai_router().

    Accepts e.g. {"OPENAI_API_KEY": ..., "OPENAI_BASE_URL": ...,
    "OPENAI_MODEL": ..., "GEMINI_USE_PROXY": "true", "DEEPSEEK_VISION": "true",
    "PROXY_URL": "http://host.docker.internal:1080"} and optional
    "task_routing": {"risk_analysis": "deepseek", ...}.
    """
    global PROXY_URL
    updated = 0
    if "PROXY_URL" in payload:
        PROXY_URL = str(payload.get("PROXY_URL") or "").strip()
        updated += 1
    # VL_MODEL 允许空串（清空=回退主模型），须在空值跳过之前处理
    for prefix, slot in (("OPENAI", "gpt"), ("DEEPSEEK", "deepseek"),
                         ("QWEN", "qwen"), ("GEMINI", "gemini"),
                         ("ZHIPU", "zhipu"), ("MOONSHOT", "moonshot")):
        vk = f"{prefix}_VL_MODEL"
        if vk in payload and isinstance(payload[vk], str):
            MODEL_CONFIG[slot]["vision_model"] = payload[vk].strip()
            updated += 1
    for env_key, value in payload.items():
        if not value or not isinstance(value, str):
            continue
        if not env_key.isupper():
            continue
        for prefix, slot in (("OPENAI", "gpt"), ("DEEPSEEK", "deepseek"),
                             ("QWEN", "qwen"), ("GEMINI", "gemini"),
                             ("ZHIPU", "zhipu"), ("MOONSHOT", "moonshot")):
            if not env_key.startswith(prefix + "_"):
                continue
            field = env_key[len(prefix) + 1:]
            if field == "API_KEY":
                MODEL_CONFIG[slot]["api_key"] = value
                updated += 1
            elif field == "BASE_URL":
                MODEL_CONFIG[slot]["base_url"] = value
                updated += 1
            elif field == "MODEL":
                MODEL_CONFIG[slot]["model"] = value
                updated += 1
            elif field == "USE_PROXY":
                MODEL_CONFIG[slot]["use_proxy"] = value.strip().lower() in (
                    "1", "true", "yes", "on")
                updated += 1
            elif field == "VISION":
                MODEL_CONFIG[slot]["vision"] = value.strip().lower() in (
                    "1", "true", "yes", "on")
                updated += 1
            elif field == "IPV6":
                MODEL_CONFIG[slot]["ipv6"] = value.strip().lower() in (
                    "1", "true", "yes", "on")
                updated += 1

    routing_updated = _apply_routing_dict(payload.get("task_routing") or {})

    if updated or routing_updated:
        _save_router_config()
    return {"config_fields": updated, "routing_rules": routing_updated}


# ============ Prompts ============

PROMPTS = {
    "risk_analysis": """你是一名专业二手交易风险分析专家。

请分析以下商品信息，检测潜在风险：
- 是否存在诈骗套路
- 是否隐藏缺陷
- 是否价格异常
- 是否商家伪装个人
- 是否为假货/翻新/矿卡

评分校准（必须遵守）：
- 标题/描述明确说明物理损坏或故障（坏板/坏的/针脚废/弯针/点不亮/进水/烧毁/故障件不退换）
  → risk_score 必须 ≥ 90。这是最高风险：买家几乎必然受损，远比"疑似引流"严重。
- 仅价格偏低、无其他疑点 → risk_score 40-60，不要仅凭低价打高分。
- 价格异常低 + 无实物图 + 描述含糊 → 70-85。

{rag_context}

商品信息：
{content}

请以JSON格式输出：
{{
    "risk_level": "low/medium/high",
    "risk_score": 0-100,
    "risk_reasons": ["原因1", "原因2"],
    "recommendation": "建议"
}}""",

    "command_parse": """你是闲鱼搜索指令解析专家。把用户的自然语言指令解析为结构化搜索参数。

核心要求：
1. keyword 必须是最纯的商品关键词——只保留商品名/型号/品类词，可直接输入闲鱼搜索框；
   绝不包含条件、限定、语气词（如「要」「没有」「的」「以下」「只要」）
2. 正确理解反向/否定语义，把用户的「不想要」转化为排除词：
   - 「不要矿卡」「排除翻新机」→ exclude 含对应词
   - 排除词用闲鱼标题里真实可能出现的最短特征词（如「升级存储」而非「没有升级存储的」）
   - 每个排除意图必须给出 2-4 个标题常见同义变体以提高命中率：
     「要没有升级存储的/要原版未改的」→ [升级存储, 扩容, 刷机, 改存储]
     「不要矿卡」→ [矿卡, 矿机, 挖矿]
     「不要翻新机」→ [翻新, 官换, 组装机]
     「不要配件」→ [配件, 钢化膜, 保护壳]
3. 商品名中的字母数字型号必须完整保留（如 oesp、b460、RTX4090、iPhone15）
4. 价格条件提取为数字（「5000以下」→ max_price=5000）；卖家偏好仅 personal/business/null

用户指令：
{content}

只输出 JSON，不要任何解释：
{{
    "keyword": "纯商品关键词",
    "exclude_keywords": ["排除特征词"],
    "max_price": 0,
    "min_price": 0,
    "seller_type": null
}}""",

    "video_analysis": """你是一名二手商品鉴定助手。请观看这个商品视频，用 2-3 句话客观描述：
1. 视频中商品的实际状态（外观/成色/配件/开机情况）
2. 与标题/描述宣称是否一致
3. 任何可疑之处（翻新痕迹/换壳/与描述不符）

只输出描述文字，不要 JSON、不要列表符号。""",

    "seller_analysis": """你是一名二手交易平台卖家分析专家。

请分析以下卖家信息，判断卖家类型：

卖家信息：
{content}

请以JSON格式输出：
{{
    "seller_type": "个人卖家/专业商家/可疑商家",
    "confidence": 0-100,
    "seller_score": 0-100,
    "reasons": ["判断依据1", "判断依据2"]
}}""",

    "price_analysis": """你是一名二手市场价格分析师。

请分析以下商品的价格合理性：

商品信息：
{content}

请以JSON格式输出：
{{
    "market_price": 预估市场价,
    "price_score": 0-100,
    "is_good_deal": true/false,
    "discount_rate": 折扣百分比,
    "analysis": "分析说明"
}}""",

    "product_analysis": """你是一名专业二手商品鉴定专家。

请综合分析以下商品：

{rag_context}

商品信息：
{content}

请从以下维度评估：
1. 商品真实性（描述与图片是否一致）
2. 卖家类型（个人/商家）
3. 风险点（诈骗/假货/隐藏缺陷）
4. 价格合理性
5. 是否值得购买

请以JSON格式输出：
{{
    "seller_type": "个人卖家/专业商家",
    "seller_score": 0-100,
    "risk_score": 0-100,
    "risk_level": "low/medium/high",
    "risk_reasons": [],
    "price_score": 0-100,
    "final_score": 0-100,
    "recommend": true/false,
    "reasons": []
}}""",

    "search_keywords": """你是一名二手商品搜索专家。

用户想找：{content}

请生成3-5个搜索关键词变体，用于在闲鱼平台搜索。

请以JSON格式输出：
{{
    "keywords": ["关键词1", "关键词2", "关键词3"],
    "category": "商品类别",
    "filters": {{"max_price": null, "condition": null}}
}}""",
}


# ============ Request/Response Models ============

class ChatRequest(BaseModel):
    task: str
    content: str
    model: Optional[str] = None
    temperature: float = 0.3
    max_tokens: int = 2000


class ChatResponse(BaseModel):
    success: bool
    task: str
    model_used: str
    result: str
    parsed: Optional[Dict[str, Any]] = None
    tokens_used: int = 0
    latency_ms: int = 0


# ============ AI Callers ============

async def call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    temperature: float = 0.3,
    max_tokens: int = 2000,
    response_format: Optional[dict] = None,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Call any OpenAI-compatible API endpoint."""
    if not api_key:
        return {"error": f"API key not configured for {model}"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format

    client_kwargs: Dict[str, Any] = {"timeout": 60}
    if proxy:
        client_kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**client_kwargs) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
        )
        # 显式解析错误体（额度/限流信息在 body 里），交给上层轮转
        if resp.status_code >= 400:
            msg = ""
            try:
                msg = resp.json().get("error", {}).get("message", "")
            except Exception:
                msg = ""
            if not msg:
                msg = resp.text[:300]
            return {"error": f"HTTP {resp.status_code}: {msg}"}
        data = resp.json()

    choice = data.get("choices", [{}])[0]
    return {
        "content": choice.get("message", {}).get("content", ""),
        "tokens": data.get("usage", {}).get("total_tokens", 0),
        "model": data.get("model", model),
    }


# ============ Gemini IPv6 通道 ============
# 背景（2026-08 实测）：代理服务器 IPv4 出口（APNIC 网段）被 Google 地理库
# 判为不支持地区 → gemini-2.5 报 "User location is not supported"；而 IPv6
# 出口正常。sing-box 为独立守护进程（GUI 不可控域名策略），因此在应用层
# 解析 AAAA，经代理以 IPv6 literal 建连，SNI/Host 保持原域名。
# 注意：上游代理对 IPv6 目标做 TLS 拦截（自签证书），v6 通道须 verify=False
# —— 与用户浏览器经同一代理的信任模型一致，且仅限 gemini 槽位使用。
_V6_DOH_DIRECT = "https://dns.alidns.com/resolve"   # 国内 DoH，直连可用
_V6_DOH_PROXY = "https://dns.google/resolve"        # 兜底，经代理防污染
_v6_cache: Dict[str, Any] = {"host": "", "addrs": [], "ts": 0.0}


async def _resolve_aaaa(host: str, proxy: Optional[str]) -> List[str]:
    """Resolve AAAA records for host (5min cache): alidns direct → dns.google via proxy."""
    now = time.time()
    if (_v6_cache["host"] == host and _v6_cache["addrs"]
            and now - _v6_cache["ts"] < 300):
        return _v6_cache["addrs"]
    addrs: List[str] = []
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(_V6_DOH_DIRECT, params={"name": host, "type": "AAAA"})
            addrs = [a["data"] for a in r.json().get("Answer", [])
                     if a.get("type") == 28]
    except Exception as e:
        logger.warning("AAAA direct DoH failed: %s", e)
    if not addrs and proxy:
        try:
            async with httpx.AsyncClient(timeout=12, proxy=proxy) as c:
                r = await c.get(_V6_DOH_PROXY, params={"name": host, "type": "AAAA"})
                addrs = [a["data"] for a in r.json().get("Answer", [])
                         if a.get("type") == 28]
        except Exception as e:
            logger.warning("AAAA proxy DoH failed: %s", e)
    if addrs:
        _v6_cache.update({"host": host, "addrs": addrs, "ts": now})
    return addrs


async def _gemini_post(url: str, payload: dict, proxy: Optional[str],
                       verify: bool = True, sni_host: Optional[str] = None,
                       host_header: Optional[str] = None) -> httpx.Response:
    kwargs: Dict[str, Any] = {"timeout": 60, "verify": verify}
    if proxy:
        kwargs["proxy"] = proxy
    headers = {"Host": host_header} if host_header else None
    extensions = {"sni_hostname": sni_host} if sni_host else None
    async with httpx.AsyncClient(**kwargs) as client:
        return await client.post(url, json=payload,
                                 headers=headers, extensions=extensions)


def _parse_data_image(url: str) -> tuple:
    """data:image/png;base64,xxxx → ('image/png', 'xxxx')；非 data URI 返回空。"""
    if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
        header, b64 = url[5:].split(";base64,", 1)
        return (header.strip() or "image/jpeg", b64.strip())
    return ("", "")


async def call_gemini(
    api_key: str,
    model: str,
    messages: list,
    temperature: float = 0.3,
    max_tokens: int = 2000,
    base_url: str = "https://generativelanguage.googleapis.com/v1beta",
    proxy: Optional[str] = None,
    ipv6: bool = False,
) -> Dict[str, Any]:
    """Call Google Gemini API.

    ipv6=True 且配置了代理时，优先走 IPv6 通道（应用层解析 AAAA + literal
    建连 + SNI 保域名），绕过代理 v4 出口被 Google 误判地区的问题；
    v6 全部失败回退普通域名方式。
    图片支持：OpenAI image_url 的 data URI 直接转 Gemini inline_data。
    """
    if not api_key:
        return {"error": "Gemini API key not configured"}

    # Convert messages to Gemini format（data URI 图片转 inline_data，最多 6 张）
    contents = []
    n_images = 0
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        content = msg.get("content", "")
        parts: List[Dict[str, Any]] = []
        if isinstance(content, list):
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text" and p.get("text"):
                    parts.append({"text": str(p["text"])})
                elif p.get("type") == "image_url" and n_images < 6:
                    url = (p.get("image_url") or {}).get("url", "")
                    mime, b64 = _parse_data_image(url)
                    if b64:
                        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
                        n_images += 1
        else:
            parts.append({"text": str(content)})
        if parts:
            contents.append({"role": role, "parts": parts})

    url = f"{base_url}/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
    }

    # ---- IPv6 通道（优先）----
    if ipv6 and proxy:
        host = base_url.split("://", 1)[-1].split("/")[0]
        addrs = await _resolve_aaaa(host, proxy)
        for addr in addrs[:2]:
            v6_url = url.replace(f"https://{host}", f"https://[{addr}]", 1)
            try:
                resp = await _gemini_post(
                    v6_url, payload, proxy, verify=False,
                    sni_host=host, host_header=host)
                if resp.status_code < 400:
                    logger.info("Gemini 经 IPv6 通道 (%s) 调通", addr)
                    return _parse_gemini_response(resp.json(), model)
                logger.warning("Gemini IPv6 %s HTTP %d，尝试下一地址",
                               addr, resp.status_code)
            except Exception as e:
                # str(e) 对 httpx 部分异常为空（如 RemoteProtocolError），用 repr 兜底
                logger.warning("Gemini IPv6 %s 失败: %s", addr, str(e) or repr(e))
        logger.info("Gemini IPv6 通道不可用，回退域名方式")

    # ---- 普通域名方式 ----
    resp = await _gemini_post(url, payload, proxy)
    if resp.status_code >= 400:
        msg = ""
        try:
            msg = resp.json().get("error", {}).get("message", "")
        except Exception:
            msg = ""
        if not msg:
            msg = resp.text[:300]
        return {"error": f"HTTP {resp.status_code}: {msg}"}
    return _parse_gemini_response(resp.json(), model)


def _parse_gemini_response(data: Dict[str, Any], model: str) -> Dict[str, Any]:
    text = ""
    candidates = data.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
    return {
        "content": text,
        "tokens": data.get("usageMetadata", {}).get("totalTokenCount", 0),
        "model": model,
    }


# ============ Core Router ============

def choose_model(task: str, override: Optional[str] = None) -> str:
    """Choose the best model for a task."""
    if override:
        slot = PROVIDER_TO_SLOT.get(override.lower(), override)
        if slot in MODEL_CONFIG:
            return slot
    return TASK_ROUTING.get(task, "deepseek")


def _candidate_models(primary: str) -> List[str]:
    """Primary model first (only when configured), then configured fallbacks.

    An unconfigured primary is skipped outright instead of burning a call on
    "API key not configured" — the chain immediately reflects what's usable.
    """
    candidates = []
    if primary in MODEL_CONFIG and MODEL_CONFIG[primary].get("api_key"):
        candidates.append(primary)
    for slot in FALLBACK_ORDER:
        if slot not in candidates and MODEL_CONFIG.get(slot, {}).get("api_key"):
            candidates.append(slot)
    return candidates


def _messages_have_images(messages: list) -> bool:
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def _messages_have_video(messages: list) -> bool:
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in (
                        "video_url", "video"):
                    return True
    return False


def _strip_videos(messages: list) -> list:
    """Remove video parts, leaving a textual note（视频分析禁用/槽位不可用时）。"""
    cleaned = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            cleaned.append(msg)
            continue
        texts = [str(p.get("text", "")) for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
        n_videos = sum(1 for p in content
                       if isinstance(p, dict)
                       and p.get("type") in ("video_url", "video"))
        text = "\n".join(t for t in texts if t)
        if n_videos:
            text += f"\n[注意：商品附带{n_videos}个视频，未做视频内容分析。]"
        cleaned.append({**msg, "content": text})
    return cleaned


def _strip_images(messages: list) -> list:
    """Remove image parts from message contents, leaving a textual note.

    Produces plain string content per message for maximum provider
    compatibility (used when no vision-capable model is configured).
    """
    cleaned = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            cleaned.append(msg)
            continue
        texts = [str(p.get("text", "")) for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
        n_images = sum(1 for p in content
                       if isinstance(p, dict) and p.get("type") == "image_url")
        text = "\n".join(t for t in texts if t)
        if n_images:
            text += (f"\n[注意：商品附带{n_images}张图片，当前模型不支持图像分析，"
                     f"仅基于文字信息判断。]")
        cleaned.append({**msg, "content": text})
    return cleaned


async def _invoke_slot(slot: str, messages: list, temperature: float,
                       max_tokens: int,
                       response_format: Optional[dict] = None) -> Dict[str, Any]:
    config = MODEL_CONFIG.get(slot, {})
    provider = config.get("provider", "")
    # 该 provider 勾选「走代理」且全局代理已配置时才经代理出站
    proxy = PROXY_URL if (config.get("use_proxy") and PROXY_URL) else None
    # 含图/视频消息且槽位配了独立视觉模型时换用（文本主模型与视觉模型解耦：
    # qwen 文本用 qwen3.7-max、视觉用 qwen-vl-max，2026-08-03 实测 VL 可用）
    model = config.get("model", "")
    if (config.get("vision_model")
            and (_messages_have_images(messages)
                 or _messages_have_video(messages))):
        model = config["vision_model"]
    if provider == ModelProvider.GEMINI:
        return await call_gemini(
            config.get("api_key", ""), model,
            messages, temperature, max_tokens,
            base_url=config.get("base_url", "https://generativelanguage.googleapis.com/v1beta"),
            proxy=proxy, ipv6=bool(config.get("ipv6")))
    return await call_openai_compatible(
        config.get("base_url", ""), config.get("api_key", ""),
        model, messages, temperature, max_tokens,
        response_format=response_format, proxy=proxy)


async def route_chat(
    task: str,
    content: str,
    model_override: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 2000,
    extra_messages: Optional[list] = None,
    _allow_parse_retry: bool = True,
) -> Dict[str, Any]:
    """Route an AI task to the appropriate model, with cross-model fallback."""
    model_name = choose_model(task, model_override)

    # Build messages
    if extra_messages is not None:
        messages = extra_messages
    else:
        system_prompt = PROMPTS.get(task, "请分析以下内容：\n{content}")
        rag_context = ""
        if task in ("risk_analysis", "product_analysis"):
            rag_context = await rag.retrieve_context(content)
        messages = [
            {"role": "system", "content": system_prompt.format(
                content=content, rag_context=rag_context)},
            {"role": "user", "content": content},
        ]

    # Vision guard（2026-08-03 起改为路由制）：图片消息只走路由表
    # vision_analysis 指定的首选槽位；"disabled" 或该槽位不可用（未配 key/
    # 未勾选视觉）时直接剥图走文本——不再在视觉槽位间轮转。
    # 首选调用失败（额度/网络）由下方「视觉链全灭剥图降级」兜底跳过。
    if _messages_have_images(messages):
        pref = TASK_ROUTING.get("vision_analysis", "disabled")
        if pref == "disabled":
            logger.info("视觉识别已禁用（vision_analysis=disabled），剥图走文本")
            messages = _strip_images(messages)
            candidates = _candidate_models(model_name)
        elif (pref in MODEL_CONFIG and MODEL_CONFIG[pref].get("api_key")
                and MODEL_CONFIG[pref].get("vision")):
            candidates = [pref]
        else:
            logger.warning("视觉路由槽位 %s 不可用（未配 key 或未勾选视觉），"
                           "剥图走文本", pref)
            messages = _strip_images(messages)
            candidates = _candidate_models(model_name)
    elif _messages_have_video(messages):
        # 视频守卫（独立路由 video_analysis）：disabled/不可用直接剥视频；
        # 可用则只用首选槽位——失败即跳过（调用方兜底），不做文本降级
        # （文本模型看视频 URL 只会回「无法观看」，纯浪费 tokens）
        pref = TASK_ROUTING.get("video_analysis", "disabled")
        if pref == "disabled":
            logger.info("视频分析已禁用（video_analysis=disabled），跳过视频")
            messages = _strip_videos(messages)
            candidates = _candidate_models(model_name)
        elif (pref in MODEL_CONFIG and MODEL_CONFIG[pref].get("api_key")
                and MODEL_CONFIG[pref].get("vision")):
            candidates = [pref]
        else:
            logger.warning("视频路由槽位 %s 不可用（未配 key 或未勾选视觉），"
                           "跳过视频", pref)
            messages = _strip_videos(messages)
            candidates = _candidate_models(model_name)
    else:
        candidates = _candidate_models(model_name)

    if not candidates:
        return {"success": False, "task": task, "model_used": model_name,
                "error": "没有任何已配置 API Key 的模型可用"}

    start_time = time.time()
    last_error = ""
    used_model = candidates[0]
    result: Dict[str, Any] = {}

    for idx, slot in enumerate(candidates):
        used_model = slot
        try:
            # 单槽位硬超时：gemini 视觉走代理，sing-box 抖动时 IPv6×2+域名
            # 三次尝试最坏 3×60s，会烧穿 pipeline 端 120s 客户端超时导致整件
            # 商品静默丢分（2026-08-03 实测）。封顶后快速轮转下一模型。
            result = await asyncio.wait_for(
                _invoke_slot(slot, messages, temperature, max_tokens),
                timeout=SLOT_TIMEOUT)
        except asyncio.TimeoutError:
            last_error = f"槽位调用超过 {SLOT_TIMEOUT}s 硬超时"
            logger.warning("AI call %s %s, trying next model", slot, last_error)
            _stats["fallbacks"] += 1
            continue
        except Exception as e:
            last_error = str(e) or repr(e)
            logger.warning("AI call failed (%s), trying next model: %s", slot, e)
            _stats["fallbacks"] += 1
            if _is_quota_error(last_error):
                _stats["quota_fallbacks"] += 1
            continue
        if result.get("error"):
            last_error = result["error"]
            if _is_quota_error(last_error):
                _stats["quota_fallbacks"] += 1
                logger.warning("Model %s 额度不足/限流，轮转下一模型: %s",
                               slot, last_error)
            else:
                logger.warning("Model %s unavailable: %s", slot, last_error)
            _stats["fallbacks"] += 1
            continue
        if idx > 0:
            logger.info("Fallback succeeded: %s -> %s", candidates[0], slot)
        break
    else:
        # 视觉链全灭时的最后兜底：剥图降级纯文本重试一轮。
        # 2026-08-03 实测：gemini 走代理被 sing-box 抖动拖死、deepseek/qwen
        # 端点实际不收 image_url（400 unknown variant）→ 视觉任务整批失败。
        # 剥图降级虽丢视觉信息，但保住文字三维分析不中断。
        if _messages_have_images(messages):
            logger.warning("视觉链全部失败，剥图降级纯文本重试（最后错误: %s）",
                           last_error)
            _stats["fallbacks"] += 1
            return await route_chat(
                task, content, model_override=model_override,
                temperature=temperature, max_tokens=max_tokens,
                extra_messages=_strip_images(messages))
        # 视频首选失败：直接失败返回，调用方（_analyze_video）跳过视频分析
        # —— 不剥视频降级，文本模型回「无法观看」是纯浪费 tokens
        if _messages_have_video(messages):
            logger.warning("视频分析首选 %s 失败，跳过视频分析（最后错误: %s）",
                           used_model, last_error)
        return {"success": False, "task": task, "model_used": used_model,
                "error": f"所有候选模型均失败，最后错误: {last_error}"}

    latency = int((time.time() - start_time) * 1000)

    # Update stats
    _stats["total_calls"] += 1
    _stats["calls_by_model"][used_model] = _stats["calls_by_model"].get(used_model, 0) + 1
    _stats["calls_by_task"][task] = _stats["calls_by_task"].get(task, 0) + 1

    # Try to parse JSON from response (balanced-brace extraction)
    content_text = result.get("content", "")
    parsed = _extract_json_block(content_text)

    # JSON 任务解析失败重试一次：模型偶发在字符串内输出未转义 ASCII 引号
    # （2026-08-03 ai_logs id=1214，risk 输出完整但 parsed=null），下游会把
    # 风险分析当中性缺失处理——高风险商品可能被误推。重试一次通常即愈；
    # 仍失败则带 parse_failed 标记返回，由下游保守降权。
    if (parsed is None and _allow_parse_retry
            and task in _JSON_TASKS and "{" in content_text):
        logger.warning("%s 输出 JSON 解析失败（%s），重试一次",
                       task, used_model)
        _stats["parse_retries"] = _stats.get("parse_retries", 0) + 1
        retry = await route_chat(
            task, content, model_override=model_override,
            temperature=temperature, max_tokens=max_tokens,
            extra_messages=extra_messages, _allow_parse_retry=False)
        if retry.get("parsed"):
            retry["retried_parse"] = True
            return retry
        retry["parse_failed"] = True
        return retry

    output = {
        "success": True,
        "task": task,
        "model_used": used_model,
        "result": content_text,
        "parsed": parsed,
        "tokens_used": result.get("tokens", 0),
        "latency_ms": latency,
    }
    if parsed is None and task in _JSON_TASKS:
        # 下游可用此标记区分「分析没跑」与「跑了但结果废了」
        output["parse_failed"] = True

    # Persist AI log (fire-and-forget, errors logged via done callback)
    _bg(db.log_ai_call(
        task, content, {"result": content_text[:2000], "parsed": parsed},
        used_model, output["tokens_used"], latency))

    return output


# ============ API Endpoints ============

@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Send an AI task to the router."""
    result = await route_chat(
        task=req.task,
        content=req.content,
        model_override=req.model,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
    )
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("error", "AI call failed"))
    return ChatResponse(**result)


@app.post("/api/config/update")
async def config_update(request: Request):
    """Hot-update model keys/urls and task routing (from feishu-agent).

    Accepts env-style keys (OPENAI_API_KEY, DEEPSEEK_BASE_URL, QWEN_MODEL, ...)
    plus optional {"task_routing": {"risk_analysis": "deepseek", ...}}.
    Persisted to disk so restarts keep the configuration.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="object body required")
    applied = _apply_env_style_update(body)
    configured = [slot for slot, c in MODEL_CONFIG.items() if c.get("api_key")]
    # 热更新后重初始化 RAG（缺口 A：此前只在 startup 初始化一次，
    # 后台改 key 后 RAG 不会重新激活，必须重启 ai-router）
    try:
        rag.configure_embedding(MODEL_CONFIG)
        _bg(rag.init_rag())
    except Exception as e:
        logger.warning("RAG re-init after config update failed: %s", e)
    return {"success": True, "applied": applied,
            "configured_models": configured, "routing": TASK_ROUTING}


# ============ 商品图片视觉分析 ============
# image_analysis 任务此前只在路由表挂载，无任何端点真正传图——分析全是纯文本
# （images 字段被当 json 文本，视觉链拿不到 image_url 消息件）。此处接通：
# 有图则下载转 data URI 拼 image_url，route_chat 的视觉守卫自动走视觉链
# (gemini→deepseek 视觉)，无图回退纯文本。

# 图片下载去重缓存：同一商品的 risk+seller 分析会各拉一次同样的图
# （2026-08-03 实测每 URL 重复 GET ×2，且并发重复拉取触发 alicdn 420 限流）。
# TTL 缓存 + in-flight 任务去重，成功 600s / 失败 60s。
_IMG_CACHE: Dict[str, tuple] = {}        # url -> (expires_ts, data_uri or "")
_IMG_INFLIGHT: Dict[str, asyncio.Task] = {}
_IMG_CACHE_OK_TTL = 600
_IMG_CACHE_FAIL_TTL = 60
# alicdn 防盗链/限流：带 Referer + UA 显著降低 420（实测裸请求 4 张挂 2 张）
_IMG_HEADERS = {
    "Referer": "https://www.goofish.com/",
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                   "Mobile/15E148 Safari/604.1"),
}


async def _download_image(url: str) -> str:
    """实际下载单张图转 data URI；失败返回 ""（由调用方负缓存）。"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers=_IMG_HEADERS) as c:
            r = await c.get(url)
            if r.status_code == 200 and r.content:
                mime = (r.headers.get("content-type")
                        or "image/jpeg").split(";")[0].strip()
                if not mime.startswith("image/"):
                    mime = "image/jpeg"
                return "data:%s;base64,%s" % (
                    mime, base64.b64encode(r.content).decode())
            logger.warning("商品图下载 HTTP %s: %s", r.status_code, url[:80])
    except Exception as e:
        logger.warning("商品图下载失败 %s: %s", url[:80], e)
    return ""


async def _fetch_one_image(url: str) -> str:
    """缓存 + in-flight 去重的单图获取。"""
    now = time.time()
    hit = _IMG_CACHE.get(url)
    if hit and hit[0] > now:
        return hit[1]
    task = _IMG_INFLIGHT.get(url)
    if task is None:
        task = asyncio.ensure_future(_download_image(url))
        _IMG_INFLIGHT[url] = task
        task.add_done_callback(lambda _t: _IMG_INFLIGHT.pop(url, None))
    result = await task
    ttl = _IMG_CACHE_OK_TTL if result else _IMG_CACHE_FAIL_TTL
    _IMG_CACHE[url] = (now + ttl, result)
    # 防膨胀：超阈值时清理过期项
    if len(_IMG_CACHE) > 2000:
        expired = [k for k, v in _IMG_CACHE.items() if v[0] <= now]
        for k in expired:
            _IMG_CACHE.pop(k, None)
    return result


async def _fetch_image_data_uris(urls: List[str], limit: int = 4) -> List[str]:
    """下载商品图转 data URI（alicdn 国内直连，无需代理）。失败/超限跳过。"""
    results = await asyncio.gather(
        *(_fetch_one_image(u) for u in urls[:limit]))
    return [r for r in results if r]


# 视频分析摘要缓存：同一商品 risk/seller/product 三个分析共享一次视频调用
# （视频 ≈15k tokens/次，绝不能逐任务重复）。成功 1h / 失败 5min。
_VIDEO_CACHE: Dict[str, tuple] = {}
_VIDEO_INFLIGHT: Dict[str, asyncio.Task] = {}


async def _analyze_video(url: str) -> str:
    """让视觉模型看商品视频出摘要；禁用/失败（额度/网络/超时）返回 "" 跳过。"""
    if TASK_ROUTING.get("video_analysis", "disabled") == "disabled":
        return ""
    messages = [{"role": "user", "content": [
        {"type": "text", "text": PROMPTS["video_analysis"]},
        {"type": "video_url", "video_url": {"url": url}}]}]
    try:
        r = await route_chat("video_analysis", url,
                             extra_messages=messages, max_tokens=400)
    except Exception as e:
        logger.warning("视频分析异常，跳过: %s", str(e) or repr(e))
        return ""
    if not r.get("success"):
        logger.warning("视频分析失败，跳过（%s）", str(r.get("error"))[:120])
        return ""
    logger.info("视频分析完成（%s, %d tokens）",
                r.get("model_used"), r.get("tokens_used", 0))
    return (r.get("result") or "").strip()[:800]


async def _get_video_summary(url: str) -> str:
    """缓存 + in-flight 去重的视频摘要获取。"""
    # disabled 判断必须在缓存之前：禁用后 1h 内的缓存摘要也不能再注入
    if TASK_ROUTING.get("video_analysis", "disabled") == "disabled":
        return ""
    now = time.time()
    hit = _VIDEO_CACHE.get(url)
    if hit and hit[0] > now:
        return hit[1]
    task = _VIDEO_INFLIGHT.get(url)
    if task is None:
        task = asyncio.ensure_future(_analyze_video(url))
        _VIDEO_INFLIGHT[url] = task
        task.add_done_callback(lambda _t: _VIDEO_INFLIGHT.pop(url, None))
    result = await task
    _VIDEO_CACHE[url] = (now + (3600 if result else 300), result)
    if len(_VIDEO_CACHE) > 500:
        expired = [k for k, v in _VIDEO_CACHE.items() if v[0] <= now]
        for k in expired:
            _VIDEO_CACHE.pop(k, None)
    return result


async def _analyze_maybe_vision(task: str, data: dict) -> Dict[str, Any]:
    """分析端点辅助：有图走视觉链，无图走纯文本（RAG 注入逻辑与 route_chat 一致）。

    视频：商品含视频且 video_analysis 启用时，先取视频摘要注入文本
    （每个商品只调一次，三个分析共享缓存）。"""
    content = json.dumps(data, ensure_ascii=False)
    video_urls = [u for u in (data.get("videos") or [])
                  if isinstance(u, str) and u.startswith("http")]
    if video_urls:
        summary = await _get_video_summary(video_urls[0])
        if summary:
            content += ("\n\n视频内容分析（AI 观看商品视频所得，"
                        "与文字描述冲突时以视频为准）：" + summary)
    image_urls = [u for u in (data.get("images") or [])
                  if isinstance(u, str) and u.startswith("http")]
    if not image_urls:
        return await route_chat(task, content)
    data_uris = await _fetch_image_data_uris(image_urls, limit=4)
    if not data_uris:
        return await route_chat(task, content)
    system_prompt = PROMPTS.get(task, "请分析以下内容：\n{content}")
    rag_context = ""
    if task in ("risk_analysis", "product_analysis"):
        rag_context = await rag.retrieve_context(content)
    user_content = [{"type": "text", "text": content}] + [
        {"type": "image_url", "image_url": {"url": du}} for du in data_uris]
    messages = [
        {"role": "system", "content": system_prompt.format(
            content=content, rag_context=rag_context)},
        {"role": "user", "content": user_content},
    ]
    logger.info("%s 含 %d 张图，提交视觉路由判定", task, len(data_uris))
    return await route_chat(task, content, extra_messages=messages)


@app.post("/api/analyze/product")
async def analyze_product(data: dict):
    """Full product analysis pipeline (parallel).

    product/risk/seller 三维含图走视觉链，price 保持纯文本（价格与图无关）。"""
    content = json.dumps(data, ensure_ascii=False)

    keys = ["product_analysis", "risk_analysis", "seller_analysis", "price_analysis"]
    raw = await asyncio.gather(
        *(_analyze_maybe_vision(k, data) if k != "price_analysis"
          else route_chat(k, content) for k in keys),
        return_exceptions=True)

    results = {}
    for k, r in zip(keys, raw):
        if isinstance(r, Exception):
            results[k] = {"success": False, "error": str(r), "parsed": None}
        else:
            results[k] = r

    return {
        "success": True,
        "results": results,
        "combined": _combine_analysis(results),
    }


@app.post("/api/analyze/risk")
async def analyze_risk(data: dict):
    """Risk analysis only (含图时走视觉链)."""
    return await _analyze_maybe_vision("risk_analysis", data)


@app.post("/api/analyze/seller")
async def analyze_seller(data: dict):
    """Seller analysis only (含图时走视觉链)."""
    return await _analyze_maybe_vision("seller_analysis", data)


@app.post("/api/analyze/price")
async def analyze_price(data: dict):
    """Price analysis only."""
    content = json.dumps(data, ensure_ascii=False)
    return await route_chat("price_analysis", content)


@app.post("/api/search/keywords")
async def generate_keywords(data: dict):
    """Generate search keywords from natural language."""
    content = data.get("query", "")
    return await route_chat("search_keywords", content)


@app.get("/api/models")
async def list_models():
    """List available models and their status."""
    models = {}
    for name, config in MODEL_CONFIG.items():
        models[name] = {
            "provider": config["provider"].value,
            "model": config["model"],
            "configured": bool(config["api_key"]),
            "vision": bool(config.get("vision")),
            "vision_model": config.get("vision_model", ""),
            "use_proxy": bool(config.get("use_proxy")),
        }
    return {"models": models, "routing": TASK_ROUTING,
            "proxy_url": PROXY_URL, "vision_order": VISION_FALLBACK_ORDER}


@app.get("/api/stats")
async def get_stats():
    """Get AI router statistics."""
    return {**_stats, "rag": rag.rag_status()}


@app.get("/api/stats/tokens")
async def get_token_stats():
    """Token 消耗统计（ai_logs 表持久聚合，容器重建不丢）。

    供 WebUI Bento 卡片：总量/今日/按模型/按任务/近 7 天趋势。"""
    total_rows = await db.fetch(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(tokens),0) AS tokens FROM ai_logs")
    total = total_rows[0] if total_rows else {}
    today_rows = await db.fetch(
        """SELECT COUNT(*) AS calls, COALESCE(SUM(tokens),0) AS tokens
           FROM ai_logs WHERE created_at >= CURRENT_DATE""")
    today = today_rows[0] if today_rows else {}
    by_model = await db.fetch(
        """SELECT COALESCE(model,'unknown') AS model, COUNT(*) AS calls,
                  COALESCE(SUM(tokens),0) AS tokens,
                  COALESCE(ROUND(AVG(latency_ms)),0) AS avg_latency_ms
           FROM ai_logs GROUP BY model ORDER BY tokens DESC LIMIT 8""")
    by_task = await db.fetch(
        """SELECT COALESCE(task_type,'unknown') AS task_type, COUNT(*) AS calls,
                  COALESCE(SUM(tokens),0) AS tokens
           FROM ai_logs GROUP BY task_type ORDER BY tokens DESC LIMIT 10""")
    by_day = await db.fetch(
        """SELECT DATE(created_at) AS day, COUNT(*) AS calls,
                  COALESCE(SUM(tokens),0) AS tokens
           FROM ai_logs WHERE created_at >= CURRENT_DATE - INTERVAL '6 days'
           GROUP BY DATE(created_at) ORDER BY day""")
    return {
        "total": {"calls": int(total.get("calls") or 0),
                  "tokens": int(total.get("tokens") or 0)},
        "today": {"calls": int(today.get("calls") or 0),
                  "tokens": int(today.get("tokens") or 0)},
        "by_model": by_model,
        "by_task": by_task,
        "by_day": [dict(r, day=str(r["day"])) for r in by_day],
        "session": _stats,
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "ai-router", "version": "2.1.0"}


# ---- 三级健康模型：liveness / readiness（详见 common/health.py） ----
def _load_health_mod():
    """延迟导入 common.health（服务子进程 cwd 在自身目录，需回溯项目根）。"""
    try:
        import sys as _sys
        from pathlib import Path as _P
        _root = str(_P(__file__).resolve().parents[2])
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from common import health as _h
        return _h
    except Exception:
        return None


@app.get("/api/health/live")
async def health_live():
    """存活探针：进程在跑即 200，不触碰任何依赖（供看门狗使用）。"""
    return {"status": "alive", "service": "ai-router"}


@app.get("/api/health/ready")
async def health_ready():
    """就绪探针：真实探测 SQLite / Chroma / AI provider 配置。"""
    _h = _load_health_mod()
    if _h is None:
        return {"service": "ai-router", "status": "unknown",
                "reasons": ["common.health 不可用"]}

    async def _chk_db():
        if not db.DATABASE_ENABLED:
            return False, "SQLite 已被 SQLITE_DISABLED 显式关闭"
        await db.fetchval("SELECT 1")
        return True, str(db.DB_PATH)

    def _chk_rag():
        st = rag.rag_status() or {}
        if st.get("enabled"):
            return True, f"documents={st.get('documents', 0)}"
        return False, str(st.get("reason") or "未启用")

    def _chk_provider():
        keys = {
            "deepseek": os.environ.get("DEEPSEEK_API_KEY", ""),
            "gemini": os.environ.get("GEMINI_API_KEY", ""),
            "qwen": os.environ.get("QWEN_API_KEY", ""),
        }
        got = [k for k, v in keys.items() if v]
        if got:
            return True, "已配置: " + ",".join(got)
        return False, "未配置任何 AI Key，AI 分析不可用"

    specs = [
        ("sqlite", _chk_db, True),
        ("ai_provider", _chk_provider, False),
        ("chroma_rag", _chk_rag, False),
    ]
    report = await _h.gather_report("ai-router", specs, version="2.1.0")
    return JSONResponse(status_code=200 if report["ready"] else 503,
                        content=report)


# ============ OpenAI-Compatible Passthrough ============

@app.post("/v1/chat/completions")
async def openai_compatible_chat(request: Request):
    """OpenAI-compatible endpoint — lets the V1 spider engine use the router
    as a drop-in OpenAI base_url. `model` may be a slot name, a provider key,
    a real model name, or "auto" (fallback chain decides).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"error": {"message": "invalid json"}})

    messages = body.get("messages", [])
    if not messages:
        return JSONResponse(status_code=400,
                            content={"error": {"message": "messages required"}})

    requested_model = str(body.get("model", "auto"))
    temperature = _safe_float(body.get("temperature"), 0.3)
    max_tokens = _safe_int(
        body.get("max_tokens") or body.get("max_completion_tokens"), 2000)
    response_format = body.get("response_format")
    if not isinstance(response_format, dict):
        response_format = None

    # Resolve which slot to try first
    slot_override: Optional[str] = None
    lowered = requested_model.lower()
    if lowered in PROVIDER_TO_SLOT:
        slot_override = PROVIDER_TO_SLOT[lowered]
    else:
        for slot, cfg in MODEL_CONFIG.items():
            if cfg.get("model") == requested_model and cfg.get("api_key"):
                slot_override = slot
                break

    primary = slot_override or next(
        (s for s in FALLBACK_ORDER if MODEL_CONFIG.get(s, {}).get("api_key")),
        "deepseek")

    # Vision guard: 图片消息按视觉链轮转（gemini → deepseek → gpt）
    if _messages_have_images(messages):
        vision = _vision_candidates(slot_override)
        if vision:
            candidates = vision
        else:
            messages = _strip_images(messages)
            candidates = _candidate_models(primary)
    else:
        candidates = _candidate_models(primary)

    if not candidates:
        return JSONResponse(status_code=503, content={
            "error": {"message": "no configured model available"}})

    last_error = ""
    for idx, slot in enumerate(candidates):
        try:
            result = await _invoke_slot(
                slot, messages, temperature, max_tokens,
                response_format=response_format
                if MODEL_CONFIG[slot]["provider"] != ModelProvider.GEMINI else None)
        except Exception as e:
            last_error = str(e)
            logger.warning("passthrough call failed (%s): %s", slot, e)
            _stats["fallbacks"] += 1
            if _is_quota_error(last_error):
                _stats["quota_fallbacks"] += 1
            continue
        if result.get("error"):
            last_error = result["error"]
            if _is_quota_error(last_error):
                _stats["quota_fallbacks"] += 1
                logger.warning("passthrough: %s 额度不足/限流，轮转: %s",
                               slot, last_error)
            _stats["fallbacks"] += 1
            continue

        _stats["total_calls"] += 1
        _stats["calls_by_model"][slot] = _stats["calls_by_model"].get(slot, 0) + 1
        _bg(db.log_ai_call(
            "v1_passthrough", json.dumps(messages, ensure_ascii=False)[:8000],
            {"result": result.get("content", "")[:2000]},
            slot, result.get("tokens", 0), 0))

        return {
            "id": f"chatcmpl-router-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL_CONFIG[slot].get("model", requested_model),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant",
                            "content": result.get("content", "")},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": result.get("tokens", 0),
            },
        }

    return JSONResponse(status_code=502, content={
        "error": {"message": f"all candidate models failed: {last_error}"}})


def _combine_analysis(results: Dict[str, Any]) -> Dict[str, Any]:
    """Combine multiple analysis results into a final decision."""
    combined = {
        "seller_type": "未知",
        "seller_score": 0,
        "risk_score": 0,
        "risk_level": "未知",
        "price_score": 0,
        "final_score": 0,
        "recommend": False,
        "reasons": [],
    }

    # Extract from product analysis
    pa = results.get("product_analysis", {}).get("parsed", {}) or {}
    if pa:
        combined["seller_type"] = pa.get("seller_type", "未知")
        combined["seller_score"] = pa.get("seller_score", 0)
        combined["risk_score"] = pa.get("risk_score", 0)
        combined["risk_level"] = pa.get("risk_level", "未知")
        combined["final_score"] = pa.get("final_score", 0)
        combined["recommend"] = pa.get("recommend", False)
        combined["reasons"] = pa.get("reasons", []) or []

    # Override with specific analyses
    ra = results.get("risk_analysis", {}).get("parsed", {}) or {}
    if ra:
        combined["risk_score"] = ra.get("risk_score", combined["risk_score"])
        combined["risk_level"] = ra.get("risk_level", combined["risk_level"])
        if ra.get("risk_reasons"):
            combined["reasons"].extend(ra["risk_reasons"])

    sa = results.get("seller_analysis", {}).get("parsed", {}) or {}
    if sa:
        combined["seller_type"] = sa.get("seller_type", combined["seller_type"])
        combined["seller_score"] = sa.get("seller_score", combined["seller_score"])

    pa2 = results.get("price_analysis", {}).get("parsed", {}) or {}
    if pa2:
        combined["price_score"] = pa2.get("price_score", 0)

    # Calculate final score if not set
    if not combined["final_score"]:
        combined["final_score"] = int(
            (100 - combined["risk_score"]) * 0.3 +
            combined["seller_score"] * 0.3 +
            combined["price_score"] * 0.4
        )
        combined["recommend"] = combined["final_score"] >= 60

    # Deduplicate reasons
    combined["reasons"] = list(dict.fromkeys(combined["reasons"]))

    return combined


# ============ Startup ============

@app.on_event("startup")
async def startup():
    _load_persisted_config()
    rag.configure_embedding(MODEL_CONFIG)
    _bg(rag.init_rag())
    configured = [s for s, c in MODEL_CONFIG.items() if c.get("api_key")]
    logger.info("AI Router 启动完成，已配置模型: %s", configured or "（无，等待热更新）")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8902))
    import logging
    class _HealthFilter(logging.Filter):
        def filter(self, record):
            return "/api/health" not in record.getMessage()
    logging.getLogger("uvicorn.access").addFilter(_HealthFilter())
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
