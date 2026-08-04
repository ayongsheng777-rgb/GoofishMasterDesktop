# -*- coding: utf-8 -*-
"""
渲染 GoofishMasterDesktop 桌面控制台的运行演示截图。
通过 Playwright 加载 desktop/ui/index.html，并注入一个模拟的 pywebview 后端 API，
使前端渲染出"服务健康运行"的真实状态，用于 README 演示图。
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "desktop" / "ui" / "index.html"
OUT = ROOT / "demo"
OUT.mkdir(exist_ok=True)

# ---- 模拟后端数据 ----
STATUS = [
    {"name": "feishu-agent",   "port": 8911, "running": True, "health": True, "pid": 18432},
    {"name": "ai-router",      "port": 8912, "running": True, "health": True, "pid": 18433},
    {"name": "agent-pipeline", "port": 8913, "running": True, "health": True, "pid": 18434},
    {"name": "spider-service", "port": 8914, "running": True, "health": True, "pid": 18435},
]

CONFIG = {
    "ports": {"feishu_agent": 8911, "ai_router": 8912, "agent_pipeline": 8913, "spider": 8914},
    "backends_enabled": True,
    "ai_configured": {"deepseek_api_key": True, "gemini_api_key": False, "qwen_api_key": True},
    "feishu_configured": False,
}

LOGS = {
    "launcher": [
        "[launcher] stage=init 编排 4 个微服务 (feishu-agent/ai-router/agent-pipeline/spider-service)",
        "[launcher] ports: 8911 8912 8913 8914 (均绑定 127.0.0.1)",
        "[launcher] 本地后端已启用: sqlite+fakeredis+chroma (零外部依赖)",
        "[launcher] stage=all_started 用时 2.41s，4/4 子进程拉起成功",
        "[watchdog] 健康检查通过: 4/4 服务 healthy",
    ],
    "feishu-agent": [
        "[feishu-agent] Uvicorn running on http://127.0.0.1:8911",
        "[feishu-agent] 管理后台已就绪，AI Key 可在页面配置",
        "[feishu-agent] GET /api/system/overview 200 (1.2ms)",
        "[feishu-agent] 当前决策模型: DeepSeek(主) + Qwen(视觉)",
    ],
    "ai-router": [
        "[ai-router] 多模型路由就绪: deepseek / qwen-turbo / qwen-vl-max",
        "[ai-router] 并发上限 AI_CONCURRENCY=5",
        "[ai-router] RAG 知识库已挂接 (embedding: text-embedding-v3)",
    ],
    "agent-pipeline": [
        "[agent-pipeline] 编排器监听 http://127.0.0.1:8913",
        "[agent-pipeline] 商品分析流水线就绪 (AI 三维分析 + 决策打分)",
        "[agent-pipeline] 看门狗: 单件超时 180s / 全局锁 1500s",
    ],
    "spider-service": [
        "[spider-service] 采集服务监听 http://127.0.0.1:8914",
        "[spider-service] 闲鱼搜索采集器就绪 (Playwright + 账号轮换池)",
        "[spider-service] 看门狗: join 900s 防止采集挂死连锁",
    ],
}


def main():
    inject = """
    window.__api = {
      get_status: () => Promise.resolve(%s),
      get_config: () => Promise.resolve(%s),
      get_logs: (target, n) => Promise.resolve((%s)[target] || []),
      start_all: () => Promise.resolve({ok:true}),
      stop_all: () => Promise.resolve({ok:true}),
      restart_service: (name) => Promise.resolve({ok:true}),
      open_data_dir: () => Promise.resolve({ok:true}),
      open_frontend: () => Promise.resolve({ok:true}),
    };
    window.pywebview = { api: window.__api };
    document.addEventListener('DOMContentLoaded', function(){
      window.dispatchEvent(new Event('pywebviewready'));
    });
    """ % (
        __import__("json").dumps(STATUS, ensure_ascii=False),
        __import__("json").dumps(CONFIG, ensure_ascii=False),
        __import__("json").dumps(LOGS, ensure_ascii=False),
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=str(Path.home() / "AppData/Local/ms-playwright"
                                / "chromium-1223/chrome-win64/chrome.exe"))
        page = browser.new_page(viewport={"width": 1180, "height": 820},
                                device_scale_factor=2)
        page.add_init_script(inject)
        page.goto(UI.as_uri())
        page.wait_for_timeout(1200)  # 等状态/日志/配置渲染完成

        app = page.query_selector(".app")
        app.screenshot(path=str(OUT / "demo-console.png"))
        print("saved:", OUT / "demo-console.png")

        # 打开"关于"弹层，演示第二张图
        page.click("#aboutBtn")
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "demo-about.png"))
        print("saved:", OUT / "demo-about.png")

        browser.close()


if __name__ == "__main__":
    sys.exit(main())
