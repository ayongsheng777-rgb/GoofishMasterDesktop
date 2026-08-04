# GoofishMasterDesktop

把原 `goofish-master` Docker 微服务系统改造为**一键桌面服务端**的独立项目。
与原 Docker 项目**完全解耦**：独立目录、独立端口（8911–8914）、独立数据目录，互不影响运行。

> 这是「桌面服务端技术计划书」的落地工程。当前进度：P0 骨架 + 启动编排器 + 桌面壳 + 冒烟验证。

## 目录结构

```
GoofishMasterDesktop/
├─ launcher.py            # 启动编排器（拉起 4 服务 / 健康检查 / 崩溃重启）
├─ desktop/app.py         # 桌面壳（pywebview + 托盘），调用 launcher
├─ common/config.py       # 配置中心（读 config/config.json）
├─ backends/local.py      # 可选：拉起嵌入式 postgres/redis/qdrant 二进制
├─ config.example.json    # 配置示例
├─ requirements.txt       # 依赖清单
├─ services/              # 复制自原项目的 4 个服务（已隔离，零关联）
│  ├─ feishu-agent/       # :8911 飞书机器人 + WebUI
│  ├─ ai-router/          # :8912 多模型路由 + RAG
│  ├─ agent-pipeline/     # :8913 编排
│  └─ spider-service/     # :8914 闲鱼采集（需 Playwright Chromium）
├─ knowledge-base/        # RAG 知识库（复制）
└─ data/                  # 运行时数据（SQLite/日志/账号态），gitignore
```

## 快速开始

```bash
cd GoofishMasterDesktop
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium        # spider 采集需要浏览器

# 启动全部后端（CLI）
python launcher.py start
# 查看状态
python launcher.py status
# 打开管理面板：浏览器访问 http://127.0.0.1:8911
# 停止
python launcher.py stop

# 或直接运行桌面壳（托盘 + WebView）
python desktop/app.py
```

首次运行会自动在 `config/config.json` 生成随机 `secret_key`。

## 配置

编辑 `config/config.json`：
- `feishu.app_id` / `app_secret`：飞书应用凭证
- `ai.deepseek_api_key` 等：AI 模型 Key
- `backends.*.enabled`：启用嵌入式本地后端（需先在 `backends/bin/` 放置二进制）

## 已知限制（当前里程碑）

- DB / Redis / Qdrant 未启动时，服务会**优雅降级**（连不上返回 None，不崩溃）；
  完整功能需启用 `backends` 或后续 P1 单用户化（SQLite/fakeredis/Chroma）。
- spider 采集依赖 Playwright Chromium，需在目标机 `playwright install chromium`。
- 桌面 GUI 在无图形环境（CI/服务器）下会自动跳过，仅启动后端。

## 与原项目的关系

仅**复制源码**到 `services/`，未修改原 `goofish-master` 任何文件，原 Docker 项目运行不受影响。
端口刻意偏移 +10，避免与本机运行的原项目（8901–8904）冲突。
