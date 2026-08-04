# -*- coding: utf-8 -*-
"""AI Config Manager — Save/load AI provider configs, fetch model lists, test connections."""
from __future__ import annotations
import json, logging, os, time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# 兜底目录必须落在项目内（Docker 遗留的 "/app/data" 在 Windows 会写到盘符根）。
DATA_DIR = Path(os.environ.get("DATA_DIR")
                or Path(__file__).resolve().parents[2] / "data" / "feishu-agent")
AI_CONFIG_FILE = DATA_DIR / "ai_config.json"

# Default provider configurations
# vision: 模型具备图像识别能力（图片分析任务纳入视觉轮转链）
# use_proxy: 该 provider 出站请求走全局代理（proxy_url），如国内访问 Gemini/OpenAI
DEFAULT_PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "models_endpoint": "/models",
        "chat_endpoint": "/chat/completions",
        "default_model": "gpt-4o",
        "selected_model": "gpt-4o",
        "enabled": False,
        "vision": True,
        "vision_model": "",
        "use_proxy": False,
        "icon": "🤖",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "models_endpoint": "/models",
        "chat_endpoint": "/chat/completions",
        "default_model": "deepseek-chat",
        "selected_model": "deepseek-chat",
        "enabled": False,
        # 2026-08-03 实测 api.deepseek.com 无视觉模型（仅 v4-flash/pro 文本），
        # 未来出 VL 型号时勾选视觉并填 vision_model
        "vision": False,
        "vision_model": "",
        "use_proxy": False,
        "icon": "🔍",
    },
    "qwen": {
        "name": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "models_endpoint": "/models",
        "chat_endpoint": "/chat/completions",
        "default_model": "qwen-max",
        "selected_model": "qwen-max",
        "enabled": False,
        # 2026-08-03 实测 qwen-vl-max 可识别图片且最省 tokens
        "vision": True,
        "vision_model": "qwen-vl-max",
        "use_proxy": False,
        "icon": "💬",
    },
    "gemini": {
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_key": "",
        "models_endpoint": "/models",
        "chat_endpoint": "/chat/completions",
        "default_model": "gemini-2.0-flash",
        "selected_model": "gemini-2.0-flash",
        "enabled": False,
        "vision": True,
        "vision_model": "",
        "use_proxy": False,
        "icon": "✨",
    },
    "zhipu": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "models_endpoint": "/models",
        "chat_endpoint": "/chat/completions",
        "default_model": "glm-4-flash",
        "selected_model": "glm-4-flash",
        "enabled": False,
        "vision": False,
        "vision_model": "",
        "use_proxy": False,
        "icon": "🧠",
    },
    "moonshot": {
        "name": "Moonshot Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": "",
        "models_endpoint": "/models",
        "chat_endpoint": "/chat/completions",
        "default_model": "moonshot-v1-8k",
        "selected_model": "moonshot-v1-8k",
        "enabled": False,
        "vision": False,
        "vision_model": "",
        "use_proxy": False,
        "icon": "🌙",
    },
}

# Task-to-model routing rules (which provider for which task)
# vision_analysis：视觉识别首选（含图消息走哪个 provider 的视觉模型）；
# 可选 "disabled" 不启用（含图消息剥图走纯文本）。
TASK_ROUTING_DEFAULT = {
    "vision_analysis": "qwen",
    # 视频分析：商品含视频且启用时，视觉模型先看视频出摘要注入分析；
    # disabled = 不启用；首选失败即跳过（视频 ≈15k tokens/次）
    "video_analysis": "qwen",
    "risk_analysis": "deepseek",
    "seller_analysis": "deepseek",
    "price_analysis": "deepseek",
    "chinese_text": "qwen",
    "simple_filter": "deepseek",
    "search_keywords": "deepseek",
    "product_analysis": "deepseek",
    "decision": "deepseek",
}


def load_ai_config() -> Dict[str, Any]:
    """Load AI configuration from file (supports encrypted-at-rest file)."""
    if AI_CONFIG_FILE.exists():
        data = None
        try:
            from crypto import read_json_file
            data = read_json_file(AI_CONFIG_FILE)
        except Exception:
            pass
        if data is None:
            try:
                data = json.loads(AI_CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error("Failed to load AI config: %s", e)
        if data is not None:
            # Merge with defaults to pick up new providers AND new fields
            # (older config files lack vision/use_proxy/proxy_url)
            for key, default in DEFAULT_PROVIDERS.items():
                if key not in data.get("providers", {}):
                    data.setdefault("providers", {})[key] = dict(default)
                else:
                    for field, fv in default.items():
                        data["providers"][key].setdefault(field, fv)
            data.setdefault("proxy_url", "")
            # 旧路由键 image_analysis 迁移为 vision_analysis（2026-08-03 视觉
            # 路由独立化）；已存在 vision_analysis 则以新键为准
            routing = data.setdefault("task_routing",
                                      dict(TASK_ROUTING_DEFAULT))
            if "vision_analysis" not in routing:
                routing["vision_analysis"] = routing.pop("image_analysis",
                                                         "qwen")
            else:
                routing.pop("image_analysis", None)
            # 视频分析路由项（2026-08-03 新增）：旧配置缺失时补默认值
            routing.setdefault("video_analysis",
                               TASK_ROUTING_DEFAULT["video_analysis"])
            return data

    return {
        "providers": {k: dict(v) for k, v in DEFAULT_PROVIDERS.items()},
        "task_routing": dict(TASK_ROUTING_DEFAULT),
        "proxy_url": "",
        "updated_at": None,
    }


def get_proxy_for(provider_key: str) -> Optional[str]:
    """Return the effective proxy URL for a provider ('' / None = direct)."""
    config = load_ai_config()
    p = config.get("providers", {}).get(provider_key, {})
    if p.get("use_proxy") and config.get("proxy_url"):
        return config["proxy_url"]
    return None


def save_ai_config(config: Dict[str, Any]) -> None:
    """Save AI configuration to file (encrypted at rest when crypto is available)."""
    config["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        from crypto import write_json_file
        write_json_file(AI_CONFIG_FILE, config)
        logger.info("AI config saved (encrypted)")
        return
    except Exception:
        pass
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AI_CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("AI config saved")


async def fetch_models(provider_key: str, api_key: str,
                        base_url: str) -> List[Dict[str, str]]:
    """Fetch available models from a provider's API."""
    if not api_key:
        return []

    proxy = get_proxy_for(provider_key)

    try:
        if provider_key == "gemini":
            return await _fetch_gemini_models(api_key, base_url, proxy)
        else:
            return await _fetch_openai_models(api_key, base_url, proxy)
    except Exception as e:
        logger.error("Failed to fetch models for %s: %s", provider_key, e)
        return []


async def _fetch_openai_models(api_key: str, base_url: str,
                                proxy: Optional[str] = None) -> List[Dict[str, str]]:
    """Fetch models from OpenAI-compatible API."""
    kwargs: Dict[str, Any] = {"timeout": 15}
    if proxy:
        kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**kwargs) as client:
        resp = await client.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()

    models = []
    for model in data.get("data", []):
        model_id = model.get("id", "")
        # Filter out non-chat models
        skip_keywords = ["embedding", "whisper", "tts", "dall-e", "text-",
                          "davinci", "babbage", "ada", "curie", "moderation"]
        if any(kw in model_id.lower() for kw in skip_keywords):
            continue
        models.append({
            "id": model_id,
            "name": model_id,
            "owned_by": model.get("owned_by", ""),
        })

    # Sort: latest/most capable first
    models.sort(key=lambda m: m["id"], reverse=True)
    return models


async def _fetch_gemini_models(api_key: str, base_url: str,
                                proxy: Optional[str] = None) -> List[Dict[str, str]]:
    """Fetch models from Google Gemini API."""
    kwargs: Dict[str, Any] = {"timeout": 15}
    if proxy:
        kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**kwargs) as client:
        resp = await client.get(
            f"{base_url}/models?key={api_key}",
        )
        resp.raise_for_status()
        data = resp.json()

    models = []
    for model in data.get("models", []):
        name = model.get("name", "").replace("models/", "")
        # Only include generative models
        if "generateContent" not in model.get("supportedGenerationMethods", []):
            continue
        models.append({
            "id": name,
            "name": model.get("displayName", name),
            "owned_by": "google",
        })

    models.sort(key=lambda m: m["id"], reverse=True)
    return models


async def test_connection(provider_key: str, api_key: str,
                           base_url: str, model: str) -> Dict[str, Any]:
    """Test connection to an AI provider."""
    if not api_key:
        return {"success": False, "error": "API Key 未填写"}

    proxy = get_proxy_for(provider_key)

    try:
        if provider_key == "gemini":
            return await _test_gemini(api_key, base_url, model, proxy)
        else:
            return await _test_openai_compatible(api_key, base_url, model, proxy)
    except Exception as e:
        # httpx 超时/连接异常的 str() 可能为空，repr 保底异常类型
        return {"success": False, "error": str(e) or repr(e)}


async def _test_openai_compatible(api_key: str, base_url: str,
                                    model: str,
                                    proxy: Optional[str] = None) -> Dict[str, Any]:
    """Test OpenAI-compatible API with a simple chat request."""
    start = time.time()
    kwargs: Dict[str, Any] = {"timeout": 30}
    if proxy:
        kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**kwargs) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 5,
            },
        )
        latency = int((time.time() - start) * 1000)

        if resp.status_code == 200:
            return {"success": True, "latency_ms": latency, "model": model}
        else:
            return {"success": False, "error": _http_error_detail(resp),
                    "latency_ms": latency}


def _http_error_detail(resp: httpx.Response) -> str:
    """Extract a useful error message from a non-200 response.

    代理/网关故障时返回的常是非 JSON 体（HTML/纯文本/空），直接 resp.json()
    会抛出 "Expecting value" 把真实状态码吞掉——先保底状态码+原文片段。
    """
    try:
        data = resp.json()
        msg = data.get("error", {}).get("message") or data.get("message")
        if msg:
            return f"HTTP {resp.status_code}: {msg}"
    except Exception:
        pass
    snippet = (resp.text or "").strip()[:200]
    return f"HTTP {resp.status_code}" + (f": {snippet}" if snippet else "（空响应体）")


async def _test_gemini(api_key: str, base_url: str,
                        model: str,
                        proxy: Optional[str] = None) -> Dict[str, Any]:
    """Test Gemini API.

    代理 v4 出口被 Google 误判地区时，先试 IPv6 通道（应用层解析 AAAA +
    literal 建连 + SNI 保域名；上游代理对 v6 做 TLS 拦截故 verify=False，
    与 ai-router 的 call_gemini 通道逻辑一致）。
    """
    start = time.time()
    url = f"{base_url}/models/{model}:generateContent?key={api_key}"
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Hi"}]}],
        "generationConfig": {"maxOutputTokens": 5},
    }

    if proxy:
        r = await _test_gemini_ipv6(url, body, base_url, proxy, model, start)
        if r is not None:
            return r

    kwargs: Dict[str, Any] = {"timeout": 30}
    if proxy:
        kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**kwargs) as client:
        resp = await client.post(url, json=body)
        latency = int((time.time() - start) * 1000)

        if resp.status_code == 200:
            return {"success": True, "latency_ms": latency, "model": model}
        else:
            return {"success": False, "error": _http_error_detail(resp),
                    "latency_ms": latency}


async def _test_gemini_ipv6(url: str, body: dict, base_url: str,
                             proxy: str, model: str,
                             start: float) -> Optional[Dict[str, Any]]:
    """Try the Gemini call through an IPv6 literal via the proxy. None = give up."""
    host = base_url.split("://", 1)[-1].split("/")[0]
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://dns.alidns.com/resolve",
                            params={"name": host, "type": "AAAA"})
            addrs = [a["data"] for a in r.json().get("Answer", [])
                     if a.get("type") == 28]
    except Exception:
        return None
    for addr in addrs[:2]:
        v6_url = url.replace(f"https://{host}", f"https://[{addr}]", 1)
        try:
            async with httpx.AsyncClient(timeout=30, proxy=proxy,
                                         verify=False) as c:
                resp = await c.post(v6_url, json=body,
                                    headers={"Host": host},
                                    extensions={"sni_hostname": host})
            latency = int((time.time() - start) * 1000)
            if resp.status_code == 200:
                return {"success": True, "latency_ms": latency,
                        "model": model, "via": "ipv6"}
        except Exception:
            continue
    return None


async def sync_to_ai_router(config: Dict[str, Any],
                             ai_router_url: str) -> Dict[str, Any]:
    """Sync AI configuration to the ai-router service."""
    # Build env-style config for ai-router
    router_config: Dict[str, Any] = {}
    providers = config.get("providers", {})

    # 全局代理（PROXY_URL 总是下发，空串表示清除，保证 WebUI 改动即时生效）
    router_config["PROXY_URL"] = (config.get("proxy_url") or "").strip()

    # 任务路由表一并下发（此前漏发：WebUI 改路由只存本地，router 端不生效）
    routing = config.get("task_routing") or {}
    if routing:
        router_config["task_routing"] = routing

    for key, provider in providers.items():
        if provider.get("enabled") and provider.get("api_key"):
            env_prefix = key.upper()
            router_config[f"{env_prefix}_API_KEY"] = provider["api_key"]
            router_config[f"{env_prefix}_BASE_URL"] = provider["base_url"]
            router_config[f"{env_prefix}_MODEL"] = provider["selected_model"]
            router_config[f"{env_prefix}_USE_PROXY"] = (
                "true" if provider.get("use_proxy") else "false")
            router_config[f"{env_prefix}_VISION"] = (
                "true" if provider.get("vision") else "false")
            # 独立视觉模型（如 qwen 文本 qwen3.7-max + 视觉 qwen-vl-max）；
            # 空串也要下发——用户清空后 router 端需回退主模型
            router_config[f"{env_prefix}_VL_MODEL"] = (
                provider.get("vision_model") or "")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{ai_router_url}/api/config/update",
                json=router_config,
            )
            return {"success": True, "synced": len(router_config)}
    except Exception as e:
        logger.warning("Failed to sync to ai-router: %s", e)
        return {"success": False, "error": str(e)}
