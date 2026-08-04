# -*- coding: utf-8 -*-
"""RAG knowledge base — Chroma (embedded) + OpenAI-compatible embeddings.

P1 单用户化：向量库由外部 Qdrant 改为进程内 Chroma
（chromadb.PersistentClient，落盘 DATA_DIR/chroma，零外部依赖）。
载入 KNOWLEDGE_DIR 的 markdown 知识文件、预计算 embedding 入库，检索相似
案例注入风险/商品分析 prompt。

Graceful degradation: 若未启用或没有可用 embedding provider，RAG 静默停用，
分析继续但不带知识库上下文。
"""
from __future__ import annotations
import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("ai-router.rag")

# 沿用 launcher 注入的 QDRANT_ENABLED 作为「向量库是否启用」开关；
# 底层已换为进程内 Chroma，不再需要 QDRANT_URL。
QDRANT_ENABLED = os.environ.get("QDRANT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
KNOWLEDGE_DIR = Path(os.environ.get("KNOWLEDGE_DIR")
                     or Path(__file__).resolve().parents[2] / "knowledge-base")
COLLECTION = "goofish_kb"

# DATA_DIR 由 launcher 注入；缺失时回退到项目内固定子目录（与 launcher._data_dir 对齐）。
# Chroma 向量库落盘到 DATA_DIR/chroma，随 exe 同级 data 目录持久化。
DATA_DIR = Path(os.environ.get("DATA_DIR")
                or Path(__file__).resolve().parents[2] / "data" / "ai-router")

# P1 单用户化：向量库由 Qdrant 改为进程内 Chroma（PersistentClient 落盘到
# DATA_DIR/chroma，零外部依赖）。维度由首次 add 的向量自动推断；检索用预计算
# embedding。模块级单例，避免每次检索都重建客户端（原 Qdrant 每查新建，已泄漏）。
_CHROMA = None


def _chroma_client():
    global _CHROMA
    if _CHROMA is None:
        import chromadb
        _CHROMA = chromadb.PersistentClient(path=str(DATA_DIR / "chroma"))
    return _CHROMA


def _chroma_query(collection, vectors, limit):
    """同步检索，由调用方包在 asyncio.to_thread 里执行。"""
    return collection.query(
        query_embeddings=vectors, n_results=limit,
        include=["documents", "metadatas", "distances"])

# Embedding config: defaults derived from OpenAI env, overridable.
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
# Provider 分支："" / "openai"(gpt,qwen) / "gemini"(原生 embedContent)
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "")
# Gemini 在本机需经 sing-box 代理访问 googleapis，且走 IPv6 通道
# （代理 v4 出口被 Google 误判地区，与 chat 同理；详见 ai-router main.py）
EMBEDDING_USE_PROXY = os.environ.get("EMBEDDING_USE_PROXY", "false").lower() == "true"
EMBEDDING_IPV6 = os.environ.get("EMBEDDING_IPV6", "true").lower() == "true"
EMBEDDING_PROXY_URL = os.environ.get("EMBEDDING_PROXY_URL",
                                     "http://host.docker.internal:1080")
# 代理（sing-box）不稳定是既定事实：失败重试 + 延时退避，防抖动把任务搞崩。
# 重试次数与间隔可用环境变量调（默认 3 次、每次间隔 2s）。
EMBEDDING_RETRY = int(os.environ.get("EMBEDDING_RETRY", "3"))
EMBEDDING_RETRY_DELAY = float(os.environ.get("EMBEDDING_RETRY_DELAY", "2"))
# embedding token 消耗累计（单次 _embed 调用内累计，写入 ai_logs 后清零）
_EMBED_TOKENS = 0

# ---- Gemini IPv6 通道（照搬 ai-router main.py 的 call_gemini 实现）----
_V6_DOH_DIRECT = "https://dns.alidns.com/resolve"
_V6_DOH_PROXY = "https://dns.google/resolve"
_v6_cache: Dict[str, Any] = {"host": "", "addrs": [], "ts": 0.0}


async def _resolve_aaaa(host: str, proxy: Optional[str]) -> List[str]:
    """解析 AAAA 记录（5min 缓存）：alidns 直连 → dns.google 经代理。"""
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

_state: Dict[str, Any] = {
    "enabled": False,
    "reason": "not initialized",
    "documents": 0,
}


def configure_embedding(model_config: Dict[str, Dict[str, Any]]) -> None:
    """Pick an embedding-capable provider from MODEL_CONFIG if not explicitly set.

    DeepSeek has no embedding API; OpenAI/Qwen expose OpenAI-compatible
    /embeddings endpoints; Gemini uses its native batchEmbedContents API
    (OpenAI-compatible /embeddings 路径在 Gemini 上不可靠).
    """
    global EMBEDDING_BASE_URL, EMBEDDING_API_KEY, EMBEDDING_MODEL
    global EMBEDDING_PROVIDER, EMBEDDING_USE_PROXY
    if EMBEDDING_BASE_URL and EMBEDDING_API_KEY and EMBEDDING_PROVIDER:
        return
    # OpenAI-compatible providers（直连，无需代理）
    for slot, model in (("gpt", "text-embedding-3-small"),
                        ("qwen", "text-embedding-v3")):
        cfg = model_config.get(slot, {})
        if cfg.get("api_key"):
            EMBEDDING_BASE_URL = cfg["base_url"]
            EMBEDDING_API_KEY = cfg["api_key"]
            EMBEDDING_MODEL = model
            EMBEDDING_PROVIDER = "openai"
            EMBEDDING_USE_PROXY = False
            return
    # Gemini：原生 embedContent，本机需走代理 + IPv6 通道访问 googleapis
    g = model_config.get("gemini", {})
    if g.get("api_key"):
        EMBEDDING_API_KEY = g["api_key"]
        # 注意：通用 EMBEDDING_MODEL 环境变量默认是 OpenAI 的
        # text-embedding-3-small，Gemini 用专属模型名 text-embedding-004，
        # 不能继承通用变量（否则 404）。
        EMBEDDING_MODEL = os.environ.get("GEMINI_EMBEDDING_MODEL", "text-embedding-004")
        EMBEDDING_PROVIDER = "gemini"
        EMBEDDING_USE_PROXY = True
        EMBEDDING_IPV6 = bool(g.get("ipv6", True))
        logger.info("RAG embedding provider=gemini (model=%s, proxy=%s, ipv6=%s)",
                    EMBEDDING_MODEL, EMBEDDING_PROXY_URL, EMBEDDING_IPV6)


def rag_status() -> Dict[str, Any]:
    return dict(_state)


# DashScope/Qwen 兼容 /embeddings 单批最多 10 行，超过直接 400
_EMBED_BATCH = 10


async def _embed(texts: List[str]) -> Optional[List[List[float]]]:
    global _EMBED_TOKENS
    if not (EMBEDDING_API_KEY and EMBEDDING_PROVIDER):
        return None
    _EMBED_TOKENS = 0
    t0 = time.time()
    try:
        if EMBEDDING_PROVIDER == "gemini":
            result = await _embed_gemini(texts)
        else:
            # OpenAI-compatible /embeddings：分批(≤10)请求并按序拼接，
            # 否则知识块 >10 时一次性提交会被 DashScope 拒(400)
            out: List[List[float]] = []
            for i in range(0, len(texts), _EMBED_BATCH):
                chunk = texts[i:i + _EMBED_BATCH]
                vecs = await _embed_openai(chunk)
                if vecs is None or len(vecs) != len(chunk):
                    return None
                out.extend(vecs)
            result = out
        if result:
            await _log_embedding(_EMBED_TOKENS, len(texts),
                                 int((time.time() - t0) * 1000))
        return result
    except Exception as e:
        logger.warning("Embedding call failed: %s", e)
        return None


async def _log_embedding(tokens: int, n_texts: int, latency_ms: int) -> None:
    """把 embedding 消耗写入 ai_logs(task_type=rag_embedding)，让 qwen 消耗可统计。

    用 await 而非 fire-and-forget create_task——启动时 Postgres 池是首次用才懒建，
    create_task 会在池未就绪时静默丢行（2026-08-03 实测 18 块 embed 未入帐）。
    """
    try:
        import db
        await db.log_ai_call(
            "rag_embedding", f"{n_texts} texts",
            {"provider": EMBEDDING_PROVIDER}, EMBEDDING_MODEL,
            int(tokens or 0), latency_ms)
    except Exception as e:
        logger.warning("embedding token log failed: %s", e)


async def _embed_openai(texts: List[str]) -> Optional[List[List[float]]]:
    """单次 OpenAI 兼容 /embeddings（≤_EMBED_BATCH 行），按 index 排序返回。"""
    global _EMBED_TOKENS
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{EMBEDDING_BASE_URL.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {EMBEDDING_API_KEY}"},
            json={"model": EMBEDDING_MODEL, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
    usage = data.get("usage") or {}
    _EMBED_TOKENS += int(usage.get("total_tokens")
                         or usage.get("prompt_tokens") or 0)
    items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in items]


async def _embed_gemini(texts: List[str]) -> Optional[List[List[float]]]:
    """Gemini 原生 embeddings API（embedContent，逐条）。

    复用与 chat 相同的 IPv6 通道：本地 sing-box 代理 + 应用层 AAAA 解析
    + IPv6 literal 建连 + verify=False（上游代理对 v6 做 TLS 拦截，自签证书）。
    逐条 embedContent 而非 batch，兼容性更广。
    """
    global _EMBED_TOKENS
    host = "generativelanguage.googleapis.com"
    proxy = EMBEDDING_PROXY_URL if EMBEDDING_USE_PROXY else None
    # embedContent 不返回 usage，按字符粗估 token（约 4 字符/token）
    _EMBED_TOKENS += sum(max(1, len(t) // 4) for t in texts)
    results: List[List[float]] = []
    for t in texts:
        url = (f"https://{host}/v1beta/models/{EMBEDDING_MODEL}"
               f":embedContent?key={EMBEDDING_API_KEY}")
        payload = {"content": {"parts": [{"text": t}]}}
        # 代理抖动重试：失败延时重试 EMBEDDING_RETRY 次，全部失败才放弃
        resp = None
        for attempt in range(max(1, EMBEDDING_RETRY)):
            resp = await _gemini_embed_one(url, payload, host, proxy)
            if resp is not None:
                break
            if attempt < EMBEDDING_RETRY - 1:
                logger.info("embedding 第%d次失败，%gs 后重试（代理抖动容错）",
                            attempt + 1, EMBEDDING_RETRY_DELAY)
                await asyncio.sleep(EMBEDDING_RETRY_DELAY)
        if resp is None:
            return None
        try:
            vals = resp.get("embedding", {}).get("values") or []
            if not vals:
                return None
            results.append([float(x) for x in vals])
        except Exception as e:
            logger.warning("Gemini embedding 解析失败: %s", e)
            return None
    return results


async def _gemini_embed_one(url: str, payload: dict, host: str,
                            proxy: Optional[str]) -> Optional[Dict[str, Any]]:
    # IPv6 通道优先（与 call_gemini 同逻辑）
    if EMBEDDING_IPV6 and proxy:
        addrs = await _resolve_aaaa(host, proxy)
        for addr in addrs[:2]:
            v6_url = url.replace(f"https://{host}", f"https://[{addr}]", 1)
            try:
                resp = await _gemini_post(
                    v6_url, payload, proxy, verify=False,
                    sni_host=host, host_header=host)
                if resp.status_code < 400:
                    return resp.json()
                logger.warning("Gemini embed IPv6 %s HTTP %d", addr, resp.status_code)
            except Exception as e:
                logger.warning("Gemini embed IPv6 %s 失败: %s", addr, e)
        logger.info("Gemini embed IPv6 通道不可用，回退域名方式")
    # 普通域名方式
    try:
        resp = await _gemini_post(url, payload, proxy)
        if resp.status_code >= 400:
            logger.warning("Gemini embedding HTTP %d: %s",
                           resp.status_code, resp.text[:200])
            return None
        return resp.json()
    except Exception as e:
        logger.warning("Gemini embedding 请求失败: %s", e)
        return None


# 该 key 实际支持的 embedding 模型名因 API 版本/区域而异，启动探测可用者
_EMBEDDING_MODEL_CANDIDATES = [
    "text-embedding-004", "embedding-001",
    "text-multilingual-embedding-002", "text-embedding-002",
]


async def _discover_embedding_model() -> Optional[str]:
    """逐个试出该 key 实际可用的 Gemini embedding 模型（embedContent）。"""
    global EMBEDDING_MODEL
    saved = EMBEDDING_MODEL
    for cand in _EMBEDDING_MODEL_CANDIDATES:
        EMBEDDING_MODEL = cand
        try:
            r = await _embed_gemini(["probe"])
            if r and r[0]:
                logger.info("RAG embedding 模型自动选定: %s (dim=%d)", cand, len(r[0]))
                return cand
        except Exception as e:
            logger.warning("embedding 候选 %s 不可用: %s", cand, str(e)[:120])
        await asyncio.sleep(1.0)  # 候选间小延时，避免打挂抖动的代理
    EMBEDDING_MODEL = saved
    return None


def _load_documents() -> List[Dict[str, str]]:
    """Load markdown knowledge files, one chunk per ## section (fallback: whole file)."""
    docs: List[Dict[str, str]] = []
    if not KNOWLEDGE_DIR.exists():
        return docs
    for path in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        category = path.parent.name
        sections: List[str] = []
        current: List[str] = []
        for line in text.splitlines():
            if line.startswith("## ") and current:
                sections.append("\n".join(current).strip())
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append("\n".join(current).strip())
        for i, sec in enumerate(s for s in sections if len(s) > 20):
            docs.append({
                "id": hashlib.md5(f"{path}:{i}".encode()).hexdigest(),
                "text": sec[:2000],
                "source": path.name,
                "category": category,
            })
    return docs


async def init_rag() -> None:
    """Ensure collection exists and knowledge files are embedded. Idempotent."""
    global _state
    # 后端未启用（桌面版默认）时直接降级，不尝试连接
    if not QDRANT_ENABLED:
        _state = {"enabled": False, "reason": "backend disabled (optional, not enabled)",
                  "documents": 0}
        logger.info("向量库(RAG) 未启用（可选组件），知识检索降级")
        return

    docs = _load_documents()
    if not docs:
        _state = {"enabled": False, "reason": "no knowledge files found",
                  "documents": 0}
        return

    # 探测可用 embedding 模型名仅 Gemini 需要（该 key 支持的名称因版本/区域
    # 而异，自动选）；openai 路径(qwen/gpt)模型已由 configure_embedding 确定，
    # 直接探测连通性即可——否则 gemini 探测会覆盖模型名并劫持到 gemini 路径
    # （2026-08-03 bug：配了 qwen 却仍走 gemini 导致 RAG 一直无法激活）
    if EMBEDDING_PROVIDER == "gemini":
        chosen = await _discover_embedding_model()
        if not chosen:
            _state = {"enabled": False,
                      "reason": "no usable gemini embedding model for this key",
                      "documents": 0}
            return
    probe = await _embed(["probe"])
    if not probe:
        _state = {"enabled": False,
                  "reason": "embedding probe failed", "documents": 0}
        return
    dim = len(probe[0])

    try:
        client = _chroma_client()
        collection = client.get_or_create_collection(
            COLLECTION, metadata={"hnsw:space": "cosine"})
        existing = collection.count()
        if existing >= len(docs):
            _state = {"enabled": True, "reason": "ok", "documents": existing}
            return

        vectors = await _embed([d["text"] for d in docs])
        if not vectors or len(vectors) != len(docs):
            _state = {"enabled": False, "reason": "embedding batch failed",
                      "documents": 0}
            return

        await asyncio.to_thread(
            collection.add,
            ids=[d["id"] for d in docs],
            embeddings=vectors,
            documents=[d["text"] for d in docs],
            metadatas=[{"source": d["source"], "category": d["category"]}
                       for d in docs],
        )
        _state = {"enabled": True, "reason": "ok", "documents": len(docs)}
        logger.info("RAG ready: %d knowledge chunks in %s", len(docs), COLLECTION)
    except Exception as e:
        logger.warning("RAG init failed (disabled): %s", e)
        _state = {"enabled": False, "reason": f"chroma error: {e}",
                  "documents": 0}


async def retrieve_context(query: str, limit: int = 3) -> str:
    """Return formatted similar knowledge for a query, or '' if unavailable."""
    if not _state.get("enabled"):
        return ""
    try:
        vectors = await _embed([query[:1500]])
        if not vectors:
            return ""
        client = _chroma_client()
        collection = client.get_collection(COLLECTION)
        res = await asyncio.to_thread(_chroma_query, collection, vectors, limit)
        docs_hits = res.get("documents") or [[]]
        metas = res.get("metadatas") or [[]]
        dists = res.get("distances") or [[]]
        if not (docs_hits and docs_hits[0]):
            return ""
        # 余弦距离 > 0.65 ≈ 相似度 < 0.35，跳过弱匹配（对齐原 Qdrant score_threshold=0.35）
        lines = ["【知识库相似案例】"]
        for text, meta, dist in zip(docs_hits[0], metas[0], dists[0]):
            if dist > 0.65:
                continue
            snippet = (text or "")[:400]
            meta = meta or {}
            lines.append(f"- ({meta.get('category', '?')}/{meta.get('source', '?')}) {snippet}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)
    except Exception as e:
        logger.warning("RAG retrieve failed: %s", e)
        return ""
