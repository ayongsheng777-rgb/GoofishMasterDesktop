# -*- coding: utf-8 -*-
"""全链路压力测试：指令解析 → pipeline 载荷/超时 → spider 展开/AI兜底/
学习/时间过滤/去重/中止 → DB 迁移。

不依赖真实浏览器/闲鱼登录/飞书凭据：采集引擎（src.scraper）与
ai-router（httpx MockTransport）全部打桩，链路逻辑真实执行。
"""
import asyncio
import importlib.util
import sys
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 模块加载辅助
# ---------------------------------------------------------------------------

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def spider_mod(tmp_path, monkeypatch):
    """加载 spider-service/main.py：DATA_DIR/词库指向 tmp，scraper 打桩。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "spider"))
    monkeypatch.setenv("KEYWORD_LEXICON_PATH", str(tmp_path / "lexicon.json"))
    monkeypatch.delenv("KEYWORD_EXPAND_ENABLED", raising=False)
    monkeypatch.delenv("KEYWORD_EXPAND_MAX", raising=False)

    calls = {"keywords": [], "error_script": {}}

    src_pkg = types.ModuleType("src")
    scraper = types.ModuleType("src.scraper")
    scraper.LAST_SCRAPE_ERROR = None
    scraper.fake_db = {}  # keyword -> items

    async def fake_scrape(task_config, debug_limit=0):
        kw = task_config["keyword"]
        calls["keywords"].append(kw)
        scraper.LAST_SCRAPE_ERROR = calls["error_script"].get(kw)
        return len(scraper.fake_db.get(kw, []))

    scraper.scrape_xianyu = fake_scrape
    src_pkg.scraper = scraper
    monkeypatch.setitem(sys.modules, "src", src_pkg)
    monkeypatch.setitem(sys.modules, "src.scraper", scraper)

    mod = _load_module("spider_main_stress",
                       ROOT / "services" / "spider-service" / "main.py")

    async def fake_baseline(keyword):
        return 0

    async def fake_load(keyword, since_id=0, limit=50, newest_first=False):
        return list(scraper.fake_db.get(keyword, []))

    monkeypatch.setattr(mod, "_get_max_result_id", fake_baseline)
    monkeypatch.setattr(mod, "_load_keyword_results", fake_load)
    mod.calls = calls
    mod.scraper = scraper
    return mod


def _item(item_id, title, days_old=0):
    ts = (datetime.now() - timedelta(days=days_old)).strftime("%Y-%m-%d %H:%M")
    return {"item_id": item_id, "title": title, "price": 100.0,
            "url": f"https://xianyu.example/{item_id}", "publish_time": ts}


def _req(mod, keyword, **kw):
    return mod.SearchRequest(keyword=keyword, ai_analysis=False, **kw)


# ---------------------------------------------------------------------------
# A. spider 链路：展开 → 去重 → 时间过滤
# ---------------------------------------------------------------------------

class TestSpiderChain:
    @pytest.mark.asyncio
    async def test_expand_dedup_timefilter(self, spider_mod):
        # 4 个变体各自回 2 条，item_a 跨变体重复 → 去重后应只算一次
        kws = ["摔坏的手机", "摔坏的iphone", "屏幕破的手机", "摔坏的苹果手机"]
        for i, kw in enumerate(kws):
            spider_mod.scraper.fake_db[kw] = [
                _item("item_dup", f"共用语 {kw}", days_old=1),       # 重复+新
                _item(f"item_{i}", f"独立 {kw}", days_old=10),       # 超期
            ]
        resp = await spider_mod.run_spider_search(
            _req(spider_mod, "摔坏的手机", publish_within_days=3))
        assert resp["status"] == "completed"
        assert spider_mod.calls["keywords"] == kws            # 4 变体全采
        assert resp["publish_within_days"] == 3
        assert resp["expanded_keywords"] == kws
        # 8 条原始 → 超期 4 条丢弃 → 剩 4 条且 item_dup 只出现一次
        ids = [r["item_id"] for r in resp["results"]]
        assert ids.count("item_dup") == 1
        assert all(r["item_id"] == "item_dup" for r in resp["results"])
        assert resp["results"][0]["_matched_keyword"] == "摔坏的手机"

    @pytest.mark.asyncio
    async def test_abort_remaining_on_login_expired(self, spider_mod):
        spider_mod.calls["error_script"]["摔坏的iphone"] = "login_expired"
        spider_mod.scraper.fake_db["摔坏的手机"] = [_item("x1", "正常", 0)]
        resp = await spider_mod.run_spider_search(
            _req(spider_mod, "摔坏的手机"))
        assert resp["login_expired"] is True
        # 第 2 个变体触发登录失效 → 第 3/4 个不再采
        assert spider_mod.calls["keywords"] == ["摔坏的手机", "摔坏的iphone"]

    @pytest.mark.asyncio
    async def test_ai_fallback_and_lexicon_learning(self, spider_mod, monkeypatch):
        """未知瑕疵词 → AI 兜底给变体 → 瑕疵词落学习库 → 二次搜索走缓存"""
        ai_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            ai_calls.append(request)
            return httpx.Response(200, json={
                "success": True,
                "parsed": {"keywords": ["屏幕有绿线的手机", "绿线的手机"]}})

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

        spider_mod.scraper.fake_db["屏幕有绿线的手机"] = [_item("g1", "绿线机", 0)]
        resp = await spider_mod.run_spider_search(
            _req(spider_mod, "屏幕有绿线的手机"))
        assert len(ai_calls) == 1
        assert "绿线的手机" in resp["expanded_keywords"]
        # 瑕疵词已入库
        from common.keyword_lexicon_store import learned_defect_families
        learned = {t for _f, terms in learned_defect_families() for t in terms}
        assert "屏幕有绿线" in learned
        # 二次搜索：静态命中学习词，不再调 AI
        resp2 = await spider_mod.run_spider_search(
            _req(spider_mod, "屏幕有绿线的手机"))
        assert len(ai_calls) == 1
        assert len(resp2["expanded_keywords"]) >= 2

    @pytest.mark.asyncio
    async def test_mining_promotes_after_two_rounds(self, spider_mod):
        """瑕疵语境结果标题里反复出现的未知表述 → 自动转正入词库"""
        for kw in ("坏的手机", "坏的手机2"):
            pass
        spider_mod.scraper.fake_db["坏的手机"] = [
            _item("m1", "手机 转轴松动 便宜出", 0)]
        await spider_mod.run_spider_search(_req(spider_mod, "坏的手机"))
        from common.keyword_lexicon_store import learned_defect_families, load
        assert "转轴松动" not in {t for _f, ts in learned_defect_families()
                                 for t in ts}
        # 第二轮再出现 → 转正（换关键词避免链接去重影响）
        spider_mod.scraper.fake_db["坏的iphone"] = [
            _item("m2", "iphone 转轴松动 急出", 0)]
        await spider_mod.run_spider_search(_req(spider_mod, "坏的iphone"))
        assert "转轴松动" in {t for _f, ts in learned_defect_families()
                             for t in ts}

    @pytest.mark.asyncio
    async def test_stop_mid_search_propagates(self, spider_mod):
        """多变体采集中途取消 → CancelledError 传播、锁释放"""
        async def slow_scrape(task_config, debug_limit=0):
            await asyncio.sleep(30)
            return 0

        spider_mod.scraper.scrape_xianyu = slow_scrape
        sys.modules["src.scraper"].scrape_xianyu = slow_scrape
        task = asyncio.create_task(
            spider_mod.run_spider_search(_req(spider_mod, "摔坏的手机")))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not spider_mod._search_lock.locked()

    @pytest.mark.asyncio
    async def test_stress_30_searches_invariants(self, spider_mod, monkeypatch):
        """压力：30 次混合搜索（瑕疵/普通/时间过滤/AI兜底失败），校验不变量"""
        # AI 路由挂掉 → 兜底必须静默
        def dead_handler(request):
            return httpx.Response(500, json={"error": "down"})

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: real_client(
                transport=httpx.MockTransport(dead_handler), **kw))

        keywords = (["摔坏的手机", "坏的笔记本电脑", "进水的iphone", "跑气的桨板"]
                    + ["iphone15", "数据线", "机械键盘"]
                    + ["屏幕有绿线的手机", "键盘不能用的笔记本"])
        t0 = time.monotonic()
        for i in range(30):
            kw = keywords[i % len(keywords)]
            for vkw in spider_mod._expand_keyword(kw):
                spider_mod.scraper.fake_db.setdefault(
                    vkw, [_item(f"s{i}_{vkw}", f"标题 {vkw}", days_old=i % 10)])
            days = 3 if i % 3 == 0 else None
            resp = await spider_mod.run_spider_search(
                _req(spider_mod, kw, publish_within_days=days))
            assert resp["status"] == "completed", f"第{i}次失败: {resp}"
            assert len(resp["expanded_keywords"]) <= 4
            assert len(resp["results"]) <= 50
            ids = [r.get("item_id") for r in resp["results"] if r.get("item_id")]
            assert len(ids) == len(set(ids)), "去重不变量被破坏"
            if days:
                for r in resp["results"]:
                    ts = r.get("publish_time")
                    if ts and ts != "未知时间":
                        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M")
                        assert datetime.now() - dt <= timedelta(days=days + 0.01)
        elapsed = time.monotonic() - t0
        print(f"\n30 次搜索链路耗时 {elapsed:.2f}s（mock 引擎）")


# ---------------------------------------------------------------------------
# B. pipeline 载荷透传与超时计算（mock spider HTTP）
# ---------------------------------------------------------------------------

@pytest.fixture()
def pipeline_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "pipeline"))
    monkeypatch.setenv("KEYWORD_LEXICON_PATH", str(tmp_path / "lexicon.json"))
    sys.path.insert(0, str(ROOT / "services" / "agent-pipeline"))
    try:
        mod = _load_module("pipeline_main_stress",
                           ROOT / "services" / "agent-pipeline" / "main.py")
    finally:
        sys.path.remove(str(ROOT / "services" / "agent-pipeline"))
    return mod


class TestPipelineChain:
    @pytest.mark.asyncio
    async def test_payload_and_timeout_scaling(self, pipeline_mod, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json
            captured["payload"] = _json.loads(request.content)
            return httpx.Response(200, json={"status": "completed", "results": []})

        real_client = httpx.AsyncClient

        def factory(**kw):
            captured.setdefault("timeouts", []).append(kw.get("timeout"))
            return real_client(transport=httpx.MockTransport(handler), **kw)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

        # 瑕疵词（静态 4 变体）→ 超时 960×4，字段原样透传
        await pipeline_mod._spider_search_with_retry({
            "keyword": "摔坏的手机", "publish_within_days": 3, "max_pages": 1})
        assert captured["payload"]["publish_within_days"] == 3
        assert captured["timeouts"][-1] == 960 * 4

        # 负缓存普通词 → 超时收紧 960×1
        from common.keyword_lexicon_store import set_ai_variants
        set_ai_variants("iphone15", [])
        await pipeline_mod._spider_search_with_retry({"keyword": "iphone15"})
        assert captured["timeouts"][-1] == 960

        # 未知词（无缓存，AI 可能补足）→ 按上限预留
        await pipeline_mod._spider_search_with_retry({"keyword": "扫地机器人"})
        assert captured["timeouts"][-1] == 960 * 4

    @pytest.mark.asyncio
    async def test_transport_retry_once(self, pipeline_mod, monkeypatch):
        """连接级失败 → 等待恢复重试一次（重试逻辑回归）"""
        attempts = []

        def handler(request):
            attempts.append(1)
            if len(attempts) == 1:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json={"status": "completed", "results": []})

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
        # 健康等待跳过
        monkeypatch.setattr(pipeline_mod, "_wait_spider_healthy",
                            lambda max_wait=150: asyncio.sleep(0))
        monkeypatch.setattr(pipeline_mod, "send_progress_notification",
                            lambda *a, **k: asyncio.sleep(0))
        resp = await pipeline_mod._spider_search_with_retry({"keyword": "数据线"})
        assert resp["status"] == "completed"
        assert len(attempts) == 2


# ---------------------------------------------------------------------------
# C. DB 迁移：老库补列 + 监控任务带时间字段
# ---------------------------------------------------------------------------

class TestDbMigration:
    @pytest.mark.asyncio
    async def test_monitor_tasks_column_migration(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "pipe"))
        sys.path.insert(0, str(ROOT / "services" / "agent-pipeline"))
        try:
            import importlib
            for m in ("monitor", "db"):
                sys.modules.pop(m, None)
            db = importlib.import_module("db")
            monitor = importlib.import_module("monitor")
        finally:
            sys.path.remove(str(ROOT / "services" / "agent-pipeline"))

        # 先建「老结构」表（无 publish_within_days 列）模拟存量库
        import aiosqlite
        db_path = db.DB_PATH
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute(
                """CREATE TABLE monitor_tasks (
                    task_id TEXT PRIMARY KEY, name TEXT, keyword TEXT,
                    max_price REAL, min_price REAL, seller_type TEXT,
                    exclude_keywords TEXT, interval_minutes INTEGER DEFAULT 30,
                    notify_open_id TEXT, created_by TEXT,
                    min_score INTEGER DEFAULT 60, status TEXT DEFAULT 'running',
                    found_count INTEGER DEFAULT 0, last_run TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")

        row = await monitor.create_task({
            "keyword": "摔坏的手机", "publish_within_days": 7,
            "interval_minutes": 30})
        assert row["task_id"]
        back = await db.fetchrow(
            "SELECT publish_within_days FROM monitor_tasks WHERE task_id=$1",
            row["task_id"])
        assert back["publish_within_days"] == 7

        # 超界 clamp
        row2 = await monitor.create_task({
            "keyword": "iphone", "publish_within_days": 99})
        back2 = await db.fetchrow(
            "SELECT publish_within_days FROM monitor_tasks WHERE task_id=$1",
            row2["task_id"])
        assert back2["publish_within_days"] == 14
        await db.close()


# ---------------------------------------------------------------------------
# D. 指令解析批量压力
# ---------------------------------------------------------------------------

class TestCommandParserStress:
    def test_batch_mixed_commands(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "cp_stress", ROOT / "services" / "feishu-agent" / "command_parser.py")
        cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cp)

        cases = [
            ("找 摔坏的手机 最近3天", "search", "摔坏的手机", 3),
            ("监控 iphone15 7天内发布 低于5000", "monitor", "iphone15", 7),
            ("找 RTX4090 个人卖家 价格8000以下 最近1天", "search", "RTX4090", 1),
            ("搜 跑气的桨板 14天内", "search", "跑气的桨板", 14),
            ("找 机械键盘", "search", "机械键盘", None),
            ("监控 macbook 最近30天", "monitor", "macbook", 14),  # clamp
        ]
        for text, action, kw, days in cases:
            cmd = cp.parse_command(text)
            assert cmd["action"] == action, f"{text} → {cmd}"
            assert cmd["keyword"] == kw, f"{text} keyword={cmd['keyword']}"
            assert cmd.get("publish_within_days") == days, f"{text} days"
