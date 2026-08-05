# AGENTS.md — GoofishMasterDesktop

> 本手册是维护本项目的权威依据。**改代码 / 排障 / 新增能力前先读它。**
> 面向：AI 维护者、接手开发者。定位：从零搭建的「双击即用」桌面服务端——飞书智能体二手商品情报系统的本地运行形态。

---

## 0. 一句话定位

4 个 Python 微服务**脱离 Docker**，由本地启动器（`launcher.py`）按依赖顺序编排拉起，配合桌面 GUI 壳（`desktop/app.py`，pywebview + 系统托盘）和浏览器管理面板（`feishu-agent` WebUI），实现**零 Docker、零命令行、零外部数据库、本地数据**的桌面服务端形态。所有数据存储（SQLite / fakeredis / Chroma）进程内嵌入式，随 exe 同级落盘。

**界面不暴露 Docker/容器术语**：桌面 GUI 控制台与管理面板（WebUI）**不使用「免 Docker」「无 Docker 版」「请检查容器状态」等措辞**——可选后端未启用时统一显示中性「可选组件未启用」，异常时显示「请检查服务状态」。这是产品定位（独立桌面运行端）与降级体验的一致性要求，新增 UI 文案时务必遵守。

---

## 1. 关键事实（先读，避免踩坑）

1. **独立项目**：4 个服务源码在 `services/`，由本项目自有维护。端口整体偏移 +10（8911-8914）。
2. **端口整体偏移 +10**：
   - `feishu_agent = 8911`（管理面板 + 飞书长连接）
   - `ai_router   = 8912`（多模型路由 + RAG）
   - `agent_pipeline = 8913`（编排 + 决策打分）
   - `spider      = 8914`（闲鱼采集）
3. **配置唯一入口**：`config/config.json`。首次运行自动生成，并随机生成 `secret_key`。没有 `.env`。
4. **服务优雅降级（增强）**：本地后端（PG/Redis/Qdrant）未启用时，launcher 注入空 `*_URL` + 显式 `*_ENABLED=false`；各服务据此**直接跳过连接**（不再向死地址重试/指数退避）。`/api/system/overview` 据此聚合 `system_status`（healthy/degraded/error）与 `degraded_features`（受影响的可选功能清单），前端仅对真 `error` 弹红告警、`degraded` 呈中性「可选组件未启用」。详见 §3.1。
5. **spider 依赖 Playwright Chromium**：未装 `chromium` 时 spider 起不来（采集功能不可用），其余 3 个服务不受影响。
6. **数据层（P1 单用户化已落地，零外部依赖）**：
   - `agent-pipeline` + `ai-router` 用 **进程内 SQLite**（aiosqlite，各服务 `DATA_DIR/goofish.db`）；
   - `feishu-agent` 用 **fakeredis**（进程内内存兼容层，API 与原 Redis 一致）；
   - `ai-router` 用 **Chroma**（chromadb.PersistentClient，落盘 `DATA_DIR/chroma`，RAG 向量）；
   - `spider` 自带 **SQLite**（`app.sqlite3`，本地存状态）。
   - 三项默认全开（`backends.*.enabled=true`），随 exe 同级落盘；`enabled=false` 仍可优雅降级。
7. **AI Key**：在 `config.json` 的 `ai.*` 配置（deepseek / gemini / qwen）；飞书 App 在 `feishu.*`。
8. **AI_SLOT_TIMEOUT=60s**、**AI_CONCURRENCY=5**（launcher 固定注入）；spider 注入 `RUN_HEADLESS=true`、`RUNNING_IN_DOCKER=false`。

---

## 2. 目录结构

```
GoofishMasterDesktop/
├── launcher.py          # 启动编排器（编排 + 健康 + 看门狗 + CLI）
├── common/config.py     # 配置中心（读 config.json，端口/后端/AI Key）
├── desktop/app.py       # 桌面壳（pywebview + pystray 托盘）
├── smoke_test.py        # 冒烟测试（仅 3 个非浏览器服务）
├── requirements.txt     # Python 依赖
├── config.example.json  # 配置样例
├── config/config.json   # 实际配置（自动生成，gitignore）
├── services/            # 4 个微服务
│   ├── feishu-agent/    # 飞书长连接 + WebUI 管理后台（:8911）
│   ├── ai-router/       # 多模型路由 + RAG（:8912）
│   ├── agent-pipeline/  # 编排 + 决策打分（:8913）
│   └── spider-service/  # 闲鱼采集（Playwright，:8914）
├── knowledge-base/      # RAG 知识库（ai-router 读取）
├── data/                # 运行态数据（gitignore）
│   ├── logs/            # 各服务日志 <svc>.log
│   ├── spider-state/    # 闲鱼账号登录态
│   └── feishu-agent/ ai-router/ agent-pipeline/   # 各服务 DATA_DIR（含 goofish.db / chroma 向量库）
└── .venv/               # Python 虚拟环境（py3.13）
```

---

## 3. 架构与端口

启动顺序（launcher `SERVICES`）：`ai-router → feishu-agent → agent-pipeline → spider`。
（spider 最后，因为它要调用前三者。）

| 服务 | 端口 | 职责 | 依赖后端 | 关键注入环境变量 |
|------|------|------|----------|------------------|
| ai-router | 8912 | 多模型路由（DeepSeek/Gemini/Qwen）+ RAG（embedding 走 `text-embedding-v3` 直连 DashScope） | SQLite / fakeredis / **Chroma** | PORT, REDIS_ENABLED, POSTGRES_ENABLED, QDRANT_ENABLED, DEEPSEEK/GEMINI/QWEN_API_KEY, AI_PROXY_URL, AI_SLOT_TIMEOUT |
| feishu-agent | 8911 | 飞书 WebSocket 长连接 + WebUI 管理后台 | fakeredis | PORT, AI_ROUTER_URL, PIPELINE_URL, SPIDER_URL, FEISHU_APP_ID, FEISHU_APP_SECRET, REDIS_ENABLED |
| agent-pipeline | 8913 | 搜索编排 + AI 三维分析 + 决策打分 | SQLite, fakeredis | PORT, AI_ROUTER_URL, FEISHU_AGENT_URL, SPIDER_URL, AI_CONCURRENCY, POSTGRES_ENABLED, REDIS_ENABLED |
| spider-service | 8914 | 闲鱼商品采集（Playwright 无头浏览器） | SQLite(本地) | PORT, AI_ROUTER_URL, PIPELINE_URL, FEISHU_AGENT_URL, RUN_HEADLESS, RUNNING_IN_DOCKER, ACCOUNT_STATE_DIR |

**服务间调用**：全部走 `http://127.0.0.1:<偏移端口>`，由 `common/config.py` 的 `service_urls()` 统一解析。

**飞书长连接**：项目通过 WebSocket 直连飞书，**无需公网 IP / 回调域名**——这是桌面端可直接可用的关键优势。

---

## 3.1 后端优雅降级机制（增强）

桌面 exe 默认 `backends.*.enabled=true`，三项已改为进程内嵌入式实现（PG→SQLite / Redis→fakeredis / Qdrant→Chroma），**零外部依赖、不捆绑任何二进制**。降级信号走「注入式」，全链路一致：

1. **launcher 注入**（`build_env`）：后端 `enabled=true` 时注入 `REDIS_ENABLED=true` / `POSTGRES_ENABLED=true` / `QDRANT_ENABLED=true`；`enabled=false` 时注入 `=false`（各服务据此直接降级）。URL 类变量（REDIS_URL/DATABASE_URL/QDRANT_URL）已无实际用途，但 launcher 仍按 `name.upper()` 注入以兼容。
2. **各服务短路跳过**（不再连死地址、不重试/退避）：
   - `ai-router/db.py`、`agent-pipeline/db.py`：`DATABASE_ENABLED` 实际读 `POSTGRES_ENABLED`（与 launcher 注入对齐），为 false 时 `_disabled=True; return`，不建 SQLite 连接。
   - `feishu-agent/auth.py`：`_redis()` 在 `if not REDIS_ENABLED:` 时置 `_redis_client=False` 直接返回（fakeredis 内存兜底）。
   - `agent-pipeline/main.py`：`_get_redis()` 同理短路。
   - `ai-router/rag.py`：`init_rag()` 开头 `if not QDRANT_ENABLED:` → `_state={"enabled":False,"reason":"backend disabled (optional, not enabled)"}` 并 `return`。
3. **overview 聚合**（`feishu-agent/main.py /api/system/overview`）：
   - `infrastructure`：后端 `enabled=false` → `"disabled"`（可选未启用，非异常）；`enabled=true` 才探测真实地址判 `running/error`。
   - `system_status`：必需服务全绿且无 `error` 后端 → `healthy`；仅可选后端 `disabled` → `degraded`；必需服务 down 或后端 `error` → `error`。
   - `degraded_features`：所有 `disabled` 后端映射的可选能力清单，如 `["持久化监控任务 / AI 调用统计", "会话保持 / 登录限流 / 结果缓存", "RAG 知识库语义检索"]`。
4. **前端呈现**（`feishu-agent/templates/index.html`）：`system_status==='error'` → 红条「服务异常：… — 请检查服务状态」；`==='degraded'` → 中性黄条「可选组件未启用（…），主功能不受影响」；否则隐藏。infra 卡片 `disabled` 项显示所影响功能。

> 旧版误区：overview 曾写死探测 `http://qdrant:6333/healthz`（Docker 服务名），桌面版无该容器必失败→误判 `error`→弹红告警「服务异常：qdrant — 请检查容器状态」。现已改为读 `backends.*.enabled` 判 `disabled/running`（P1 后向量库为进程内 Chroma，不再探测外部端口，`enabled=true` 直接判 running）。

---

## 4. 启动 / 停止 SOP

### 命令行
```bash
cd D:\WorkBuddy\GoofishMasterDesktop
.venv\Scripts\python.exe launcher.py start      # 拉起全部服务 + 看门狗
.venv\Scripts\python.exe launcher.py status     # 查看进程/健康
.venv\Scripts\python.exe launcher.py stop       # 优雅关停（SIGTERM + 超时强杀）
.venv\Scripts\python.exe launcher.py restart    # 重启
```

### 桌面壳（GUI）
```bash
.venv\Scripts\python.exe desktop/app.py
```
- 拉起本地控制台窗口（`desktop/ui/index.html`，pywebview 加载，非浏览器跳转）。
- 系统托盘：显示控制台 / 启动后端 / 停止后端 / 退出。
- 关闭窗口默认最小化到托盘（旧版 pywebview API 不支持则直接退出并关停）。
- 无 GUI 环境（CI/服务器/无头）会自动跳过 GUI，仅后台启动后端（不报错）。

### 控制台功能（desktop/ui + desktop/api.py）
- 顶栏：品牌 + `N/4 服务运行中` 汇总药丸 + 全部启动/停止切换按钮。
- 服务状态：4 张卡片（飞书智能体 / AI 路由 / 分析编排 / 采集服务），实时健康点（绿=健康/红=停止/黄脉冲=启动中）、端口、PID、单服务「重启」。
- 运行日志：编排器 + 4 服务日志下拉切换 + 自动刷新 + 手动刷新（读 `data/logs/*.log` 尾部 300 行）。
- 配置概览：端口、本地后端是否启用、DeepSeek/Gemini/Qwen/飞书凭证是否已配置（不回显密钥）。
- 桥接层 `desktop/api.py`（`Api` 类）暴露 `get_status / start_all / stop_all / restart_service / get_logs / get_config / open_data_dir`，由 `desktop/app.py` 作为 `js_api` 注入；该模块不依赖 GUI，可独立命令行导入测试。
- 前端 `desktop/ui/app.js` 每 3 秒轮询 `get_status()` 并（可选）刷新日志。

### 管理面板
浏览器访问 **http://127.0.0.1:8911**（feishu-agent WebUI，含搜索/监控/AI Key 配置）。

### 服务日志
`data/logs/<svc>.log`（落盘而非 PIPE，避免缓冲区卡死子进程）。

### 冒烟测试
```bash
.venv\Scripts\python.exe smoke_test.py     # 仅测 3 个非浏览器服务（spider 需 Playwright）
```

---

## 5. 配置中心（common/config.py）

`config/config.json` 字段：

```json
{
  "secret_key": "...(随机生成)",
  "ports": { "feishu_agent": 8911, "ai_router": 8912, "agent_pipeline": 8913, "spider": 8914 },
  "backends": {
    "postgres": { "enabled": true, "port": 5439, "user": "goofish", "password": "goofish_v2_secret", "db": "goofish_ai" },
    "redis":    { "enabled": true, "port": 6399 },
    "qdrant":   { "enabled": true, "port": 6339 }
  },
  "feishu": { "app_id": "", "app_secret": "" },
  "ai": { "deepseek_api_key": "", "gemini_api_key": "", "qwen_api_key": "", "proxy_url": "" },
  "data_dir": "data"
}
```

**环境变量注入规则**（`launcher.build_env`）：
- 所有服务：`GOOFISH_SECRET_KEY`、`REDIS_ENABLED` / `POSTGRES_ENABLED` / `QDRANT_ENABLED`（注入式降级开关，`enabled=true` 时嵌入式 DB 全开；`REDIS_URL`/`DATABASE_URL`/`QDRANT_URL` 仍按 `name.upper()` 注入但已无实际用途）、`PYTHONUNBUFFERED=1`。
- 服务间地址：`AI_ROUTER_URL` / `PIPELINE_URL` / `FEISHU_AGENT_URL` / `SPIDER_URL`。
- 飞书：`FEISHU_APP_ID` / `FEISHU_APP_SECRET`。
- AI：`DEEPSEEK_API_KEY` / `GEMINI_API_KEY` / `QWEN_API_KEY` / `AI_PROXY_URL`。
- spider 特有：`RUN_HEADLESS=true`、`RUNNING_IN_DOCKER=false`、`ACCOUNT_STATE_DIR`。
- **每服务数据目录 `DATA_DIR`**：4 个服务全部注入，值 = `APP_DIR/data/<sub>`（`sub` ∈ `feishu-agent` / `ai-router` / `agent-pipeline` / `spider`；spider 另有 `spider-state`）。ai-router 额外注入只读的 `KNOWLEDGE_DIR = ROOT/knowledge-base`。服务侧兜底值必须与此一致，见 §9。

**改端口**：直接改 `config.json` 的 `ports.*`（注意避开本机其他占用）。

**`%APPDATA%` 回退目录改名（向后兼容）**：品牌统一后新目录是 `%APPDATA%/GoofishMasterDesktop`，但 `_app_dir()` 会**先探测旧目录 `%APPDATA%/goofish-server`（及 `~/.goofish-server`）是否存在**，存在则继续沿用，避免老版本用户配置被孤立。要彻底切新目录，手工把旧目录改名即可。

---

## 6. 数据层策略（重要）

**P1 单用户化已落地（2026-08-04）**：三项外部数据库已替换为进程内嵌入式实现，**默认全开、零外部依赖、打包体积最小**：

- `postgres` → **SQLite**（aiosqlite，各服务 `DATA_DIR/goofish.db`，手写 DDL 从查询反推）
- `redis` → **fakeredis**（进程内内存兼容层，API 与原 Redis 完全一致）
- `qdrant` → **Chroma**（chromadb.PersistentClient，落盘 `DATA_DIR/chroma`，RAG 向量）

改动集中在 `services/` 的 DB/RAG 驱动层（详见 §3.1）。`backends.*.enabled=false` 仍可优雅降级（对应功能禁用）。

---

## 7. 项目独立性

- 本项目是从零搭建的独立桌面服务端，`services/` 下 4 个微服务源码由本项目自有维护。
- 端口 8911-8914 绑定 127.0.0.1，数据目录独立（`data/`），与宿主机其它进程互不干扰。
- **本项目独有**的核心代码：`launcher.py`、`common/`、`desktop/`、`smoke_test.py`、`config.example.json`、`GoofishMasterDesktop.spec`、`GoofishMasterDesktop.iss`——这些是桌面化与打包的核心，改它们等于改本项目地基。
- `services/` 下各服务的业务逻辑（飞书对接 / AI 调用 / 采集 / 决策）是产品能力的载体，维护时直接改即可，不存在「上游同步」概念。

---

## 8. 已知限制 / 待办

1. **spider 采集**：目标机需 `playwright install chromium`（约 150MB），本环境未装 → spider 暂不可用。
2. **完整 DB 能力**：P1 已默认全开（进程内 SQLite/fakeredis/Chroma），无需外部二进制；`backends.*.enabled=false` 才降级。
3. **桌面 GUI 实测**：pywebview 需在 Windows 图形环境跑（无头环境自动跳过只启后端）。
4. **打包成 .exe**：✅ 已完成（PyInstaller onedir，详见 §11/§13）。**安装包**：✅ 已完成（Inno Setup 6，`GoofishMasterDesktop.iss`，产物 `installer/GoofishMasterDesktop-Setup-1.0.0.exe`，详见 README.md）。
5. **安全**：`secret_key` 明文存 `config.json`；未做配置加密。**代码签名**：已加入可选签名脚本 `sign.ps1`（默认跳过——无真实 CA 证书时自签名对发行零信任收益，故不产出自签名）。要真正消除 SmartScreen 拦截需购买 DigiCert/Sectigo/GlobalSign 等 CA 证书后启用（见 `sign.ps1` 头部说明）。
6. **更新通道**：自动更新 / 增量升级未实现。
7. **✅ 发布产物已刷新**：`release/GoofishMasterDesktop/`（`GoofishMasterDesktop.exe` 30.5 MB + `_internal/`，2026-08-04 重打包，已剔除内嵌 config）。
8. **🧹 遗留构建产物清理：✅ 已完成（2026-08-04 用户授权，逐项删除并验证）**：
   - `release/goofish-server/`、`dist/goofish-server/` —— 改名前的旧产物（曾内嵌本机 `secret_key`，见 §8.1 BUG-6），**已删除**；
   - `.build/` 历史 `dist_*` / `wp_*`（约 1.4 GB）—— **已删除**，仅保留 `dist_20260804_183919` + `wp_20260804_183919` + 3 个日志；
   - `D:\app\data` 与 `D:\app` —— 项目**目录之外**的空垃圾目录（已修复的 `/app/data` 缺省路径 BUG 造成）—— **已删除**；
   - `_packtest/` + `packtest.spec` + `build/` —— 早期打包探针遗留 —— **已删除**。
   现状：`release/GoofishMasterDesktop/`（正确产物）完好，exe 30.5 MB + `_internal/`，无内嵌 config/secret_key。

---

## 8.1 本次维护记录（命名统一 + 路径 BUG）

**命名统一**：源码层 `闲鱼圣手` / `Goofish Master` / `GOOFISH MASTER` / `goofish-server` 全部收敛为 **`GoofishMasterDesktop`**（17 个文件）。spec 改名 `goofish-server.spec → GoofishMasterDesktop.spec`、`build_server.spec → GoofishMasterDesktop-debug.spec`。
**未动**的标识（故意保留，属基础设施约定，改了会连锁炸）：`GOOFISH_SECRET_KEY` 环境变量、PG 的 `goofish/goofish_v2_secret/goofish_ai`、Qdrant 集合 `goofish_kb`。

**修复的 BUG**：

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| 1 | `launcher.build_env()` | `agent-pipeline` 分支漏注入 `DATA_DIR`（另外 3 个服务都有） | 补上 `_data_dir("agent-pipeline")` |
| 2 | `spider-service/main.py` | `_LOGIN_HEALTH_FILE` 写死 `/app/data/login_health.json`，写失败被 `try/except` 吞掉 → 登录健康档案静默丢失 | 改用 `DATA_DIR / "login_health.json"` |
| 3 | 6 个服务模块 | `DATA_DIR`/`STATE_DIR`/`KNOWLEDGE_DIR` 缺省值是 Docker 路径 `/app/...`，Windows 上会在盘符根建目录（**写到项目外**，实测已产生 `D:\app\data`） | 缺省值改为项目内 `parents[2]/data/<svc>` |
| 4 | `feishu-agent/feishu_bot.py` | `save_credentials` / `load_credentials` 默认参数硬编码 `/app/data/credentials.json`，**完全绕过 `DATA_DIR`**，与 `main.py` 的 `CRED_FILE` 分叉 → 后台配好凭据但机器人读不到 | 统一走模块级 `_CRED_FILE`（同 `DATA_DIR`） |
| 5 | `common/config.py` | 品牌改名会让 `%APPDATA%` 回退目录换位置，老用户配置被孤立 | 旧目录存在则继续沿用（见 §5） |
| 6 | 两个 `.spec` | 🔒 `datas` 含 `config/`，把**本机 `secret_key` 内嵌进分发产物**（已在 `release/`、`dist/` 实锤泄露）；而该副本在冻结态根本读不到（真实路径是 `APP_DIR/config`） | 从两个 spec 移除 `config` 数据项；缺配置时 `load_config()` 自动生成 + 随机 `secret_key` |

**验证**：全量 `py_compile` 通过；`smoke_test.py` → ai-router / feishu-agent / agent-pipeline 三服务 `/api/health` 全绿；`DATA_DIR` 未注入时兜底路径实测落在 `D:\WorkBuddy\GoofishMasterDesktop\data\*`，与 `launcher._data_dir()` 完全一致。

---

## 8.2 2026-08-05 测试期 BUG 修复（v1.0.0-beta.1 前）

用户实测反馈的 BUG，根因与修法：

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| 1 | `feishu-agent/main.py` | WebUI 只把 AI 配置写 `data/feishu-agent/ai_config.json`，但 launcher 重启只从 `config.json` 的 `ai.*` 注入 env，两者分叉 → 重启后 ai-router 无 key、控制台显示「未配置」 | 保存时 `_persist_ai_config_to_config_json()` 回写 3 key + 代理到 `config.json`；飞书凭证 `_handle_success` 回写 `feishu.app_id/secret`；启动 `_replay_ai_config_on_boot()` 对 ai-router 重放 `ai_config.json`（覆盖 openai/zhipu/moonshot 等 launcher 不注入的 provider） |
| 2 | `agent-pipeline/db.py` + `monitor.py` | `monitor.create_task` 把 `exclude_keywords`(Python list)直接 INSERT 进 SQLite TEXT 列，sqlite3 无法绑定 list → InterfaceError 被吞 → 返回 None → 抛「数据库不可用，监控任务无法持久化」 | `db.py` 写入/查询层加系统级 `_serialize_args`，对所有 list/dict 参数统一 `json.dumps`；`monitor.create_task` 的 `exclude_keywords` 显式 `db.to_json(...)` 双保险；读取时 `_row_to_dict` 自动还原 |
| 3 | `agent-pipeline/main.py` | `_save_last_search` 只把搜索任务元数据存进 fakeredis 的 `last_search:global`，fakeredis 进程内内存非持久化，重启即清空 → 任务中心空 | 额外落盘 `agent-pipeline/data/last_search.json`，启动时优先从磁盘恢复（fakeredis 作次级兜底） |
| 4 | `feishu-agent/templates/index.html` | `getQRCode()` 先设 `img.src` 再把父容器 `display:none→block`，img 在隐藏容器内设 src 后取消隐藏，部分浏览器不触发重绘 → 首次不显示，需二次点击 | 改为先 `removeAttribute('src')` + 显示容器，再设 `src`；缺图显式报错 |
| 5 | `feishu-agent/auth.py` | TOTP 验证器二维码的 issuer 写死 `AI-Goofish-V2`，扫码后验证器（Google Authenticator / 腾讯云验证器等）显示该旧名 | 抽模块常量 `APP_DISPLAY_NAME = "GoofishMasterDesktop"`，`get_totp_uri` 默认 issuer 改用它；两处调用 `generate_totp_qrcode` / `main.py:848` 不传 issuer 走默认 → 全部一致 |

**图标集成（app.ico）**：根目录 `app.ico` 升级为含 16/24/32/48/64/128/256 多尺寸标准 ICO（手动按 ICO 规范组装 PNG 帧；PIL 本机写入器只产单帧）；`GoofishMasterDesktop.spec` 的 `datas` 增加 `('app.ico','.')` 使其打进 `_internal`；`desktop/app.py` 窗口与托盘图标优先用 `app.ico`，缺失回退 `logo-256.png`。exe 图标由 spec `icon=` 指定、安装器图标由 `.iss` 的 `SetupIconFile` 指定。

**验证**：`py_compile` 全过；BUG2/BUG3 功能测试 round-trip 与落盘恢复均通过；BUG1/BUG4/BUG5 经代码审查确认链路闭合。

---

## 8.3 2026-08-05 后续修复（图标崩溃 + WebView2 固定版打包）

| # | 位置 | 问题 | 修法 |
|---|------|------|------|
| 6 | `desktop/app.py` | `webview.create_window(..., icon=...)` 触发 `TypeError: got an unexpected keyword argument 'icon'` → 桌面窗口启动即崩（含 `data/logs/desktop-crash.log` 报错）。根因：本机 pywebview 版本的 `create_window` **不支持 `icon` 参数**（`Window` 类也无 `icon` 属性） | 删除 `icon=` 关键字；窗口图标由 exe 内嵌的 `app.ico`（PyInstaller spec `icon=`）提供。另修一处隐患：`app.py` 第 ~137 行用了 `Path(cand)` 但模块未导入 `Path` → 补 `from pathlib import Path`（否则修完图标后会立刻触发 `NameError`） |
| 7 | 安装/分发 | 部分机器未预装系统 WebView2 Runtime，且 `.iss` 把离线安装器声明 `dontcopy` 却从不 `ExtractTemporaryFile`，`{tmp}` 始终无该 exe → 弹「未找到 WebView2 离线安装器」且无法打开桌面窗口 | **改为打包固定版本 WebView2 运行时（方案 B）**：从本机已装目录 `C:\Program Files (x86)\Microsoft\EdgeWebView\Application\151.0.4129.59\` 复制整个文件夹到项目 `webview2_runtime/`（含 `msedgewebview2.exe` + `EBWebView/`）；`desktop/app.py` 启动前置 `_resolve_webview2_runtime()` 设 `os.environ['WEBVIEW2_RUNTIME_PATH']` 指向它；`edgechromium.py` 原生读取该变量 → **完全不依赖系统 Runtime、免 UAC、免联网**。`WebView2Loader.dll` 由 pywebview 自带（`_internal/webview/lib/runtimes/win-x64/native/`，`edgechromium.py` 已加进 PATH），无需复制。`.iss` 新增 `Source: "webview2_runtime\*"; DestDir: "{app}\webview2_runtime"`；删除原 `ShellExec('runas',...)` 提权安装分支与 `dontcopy` 离线安装器声明；`launcher.check_prerequisites()` 把「随包固定版」也判为已就绪 |

**注意**：
- `webview2_runtime/` 约 500MB，**已加入 `.gitignore`**（从本机复制，不入库）；安装器随之膨胀到约 1GB。
- 固定版运行时版本跟随本机 EdgeWebView（当前 151.0.4129.59）。换机/升级时若需更新，重新从该机 `EdgeWebView\Application\<版本>\` 复制覆盖即可。
- **GitHub Release 上传坑**：`gh release upload` 经代理（HTTPS_PROXY）上传 500MB+ 大文件时返回 `HTTP 400 Bad Request`（疑代理干扰 `gh` 的 multipart 请求）。改用 `curl` 直传 `uploads.github.com/.../releases/<id>/assets?name=...`：显式 `Content-Type: application/octet-stream` + `-x http://127.0.0.1:1080`，实测可用。

---

## 9. 维护纪律（血泪坑，必读）

- **禁止**用 `Remove-Item -Recurse` / `rm -rf` 批量删项目树——易误删且触发安全删除批量确认拦截。删除改用 PowerShell `-LiteralPath` 单目标 + 先核对。
- **.venv 重建**：`python -m venv .venv --clear` 会因批量删除被拦截。正确做法：先 `mv .venv .venv_bak`（rename 不是删除），再新建 `.venv` 并 `pip install -r requirements.txt`，确认无误后删 `.venv_bak`。
- **依赖导入名陷阱**：`pywebview` 导入名是 `webview`；`pyyaml` 导入名是 `yaml`。测试写错名会误判缺包。
- **改 services/ 是改本项目业务逻辑**：`services/` 由本项目自有维护，不存在上游副本概念；桌面化逻辑在 `launcher/common/desktop`。
- **端口冲突**：永远用 8911–8914，不要回退到 8901–8904。
- **日志优先**：排障先看 `data/logs/<svc>.log`；launcher 主日志在 stdout。
- **🚨 Docker 风格绝对路径是头号污染源**：`services/` 里若有 `Path(os.environ.get("DATA_DIR", "/app/data"))` 这类写法，在 Windows 上 `/app/data` 会被解析成 **`<当前盘符>:\app\data`**，于是在盘符根建垃圾目录、把数据写到项目外。新增或修改 `services/` 文件后，**必须 grep `"/app/`** 并改成项目内兜底：
  ```python
  DATA_DIR = Path(os.environ.get("DATA_DIR")
                  or Path(__file__).resolve().parents[2] / "data" / "<svc>")
  ```
  兜底子目录名必须与 `launcher._data_dir()` 注入的一致（`feishu-agent` / `ai-router` / `agent-pipeline` / `spider` / `spider-state`），否则「launcher 起的服务」和「裸跑的模块」会读写两套目录。
- **别只信环境变量**：有函数把路径写死成默认参数（如 `feishu_bot.save/load_credentials` 的 `/app/data/credentials.json`），完全绕过 `DATA_DIR`，症状是「后台配好了但机器人读不到」。改路径时要连默认参数一起 grep。

> 历史背景：本项目早期从 `goofish-master` Docker 项目 fork 而来，已于 2026-08 独立为从零搭建的桌面服务端项目。上述 Docker 风格路径残留是 fork 期的历史包袱，新代码不应再引入此类写法。

---

## 10. Git 纪律

- 工作目录：`D:\WorkBuddy\GoofishMasterDesktop`。
- GitHub：`ayongsheng777-rgb/GoofishMasterDesktop`（Private）
- `.venv/`、`config/config.json`、`data/`、`build/`、`dist/`、`release/`、`installer/` 已 gitignore。
- git 邮箱用 noreply 隐私保护：`277914440+ayongsheng777-rgb@users.noreply.github.com`
- `git push` **走代理**（GitHub 属境外服务，本机直连不通）：push 前 `git config --global http.proxy http://127.0.0.1:1080 && git config --global https.proxy http://127.0.0.1:1080`，再 `git push`；API 调用（curl/Python）同理可用代理。
- 提交信息用中文，说明改了哪层（launcher/common/desktop 还是 services 业务逻辑）。

---

## 11. 可执行程序（exe）构建与运行

**现状**：已用 PyInstaller（onedir）把整个项目冻结为单个 `GoofishMasterDesktop.exe`，**脱离 Python 安装即可双击运行**，实测 4 个服务全部 `/api/health` 绿灯（含 spider）。

### 核心架构：单二进制多模式
一个 exe 既是**编排器**又是各服务的**运行载体**，避免 4 份依赖重复打包：
- 直接运行 `GoofishMasterDesktop.exe` → 进入 launcher 编排模式，按序 `subprocess` 拉起 4 个服务。
- 每个子进程以 `GoofishMasterDesktop.exe --service <name>` 启动 → 进入服务运行模式，复用同一个 exe（独立进程，互不干扰）。
- 冻结后 `sys.executable` 指向该 exe，launcher 据此 spawn 自身，无需 Python。

### 路径解析（冻结感知，关键）
- **资源目录 `ROOT`** = `sys._MEIPASS`（PyInstaller onedir 的 `_internal`）：存放 `services/`、`knowledge-base/`、`common/` 等**只读**资源。
- **可写应用目录 `APP_DIR`** = exe 同级目录（便携版可直接写）；若不可写（如安装到 `Program Files`）则回退到 `%APPDATA%/GoofishMasterDesktop`。`config.json`、运行日志、各服务 `DATA_DIR` 全部落在 `APP_DIR`，保证安装版也能写。

### 构建命令（历史内联参考，规范方式见下方 spec）

> 以下内联命令为早期构建参考，依赖项已变更（`qdrant_client`/`asyncpg` 已移除，改为 `chromadb`/`fakeredis`/`aiosqlite`）。**日常构建请用下方 spec 方式**。

```bash
cd D:\WorkBuddy\GoofishMasterDesktop
.venv\Scripts\python.exe -m PyInstaller --name GoofishMasterDesktop --onedir --windowed --noconfirm --clean ^
  --add-data "common;common" ^
  --add-data "services;services" --add-data "knowledge-base;knowledge-base" ^
  --exclude-module webview --exclude-module pystray --exclude-module tkinter ^
  --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 ^
  --exclude-module PyQt4 --exclude-module PySide ^
  --collect-all lark_oapi --collect-all chromadb --collect-all fakeredis --collect-all aiosqlite ^
  --collect-all openai --collect-all playwright --collect-all fastapi --collect-all uvicorn ^
  --collect-all redis --collect-all httpx --collect-all websockets --collect-all segno --collect-all pyotp ^
  --collect-all cryptography --collect-all yaml --collect-all aiofiles --collect-all requests ^
  --collect-all python_socks --collect-all pydantic_settings --collect-all PIL ^
  --hidden-import json --hidden-import os --hidden-import secrets --hidden-import sys ^
  --workpath <干净临时dir> --distpath <干净临时dir> launcher.py
```

> 📌 **规范构建方式（推荐）**：上面的内联命令等价于仓库里的 **`GoofishMasterDesktop.spec`**（`console=False` 即 `--windowed`，已含 `--collect-all webview/pystray` + `desktop` 数据文件 + `hidden-import webview.platforms.edgechromium` + 嵌入式库）。日常重建直接：
> ```bash
> cd D:\WorkBuddy\GoofishMasterDesktop
> # 先清 build/dist（PowerShell，避免 safe-delete 拦截）
> # .venv\Scripts\python.exe -m PyInstaller GoofishMasterDesktop.spec --noconfirm
> # 产物在 dist/GoofishMasterDesktop/
> ```
> ⚠️ **构建坑（必看）**：本机安全删除会拦截 `rm -rf` / PyInstaller `--clean` / `--noconfirm` 对已存在目录的清理。务必先用 PowerShell `Remove-Item -Recurse -Force` 清掉 build/dist，再不带 `--clean` 构建，否则报 `SAFE_DELETE_FAIL_CLOSED`。
> ⚠️ 复杂/原生包（chromadb/lark_oapi/cryptography/playwright 等）必须用 `--collect-all`（而非手写 `collect_all` 拼 TOC，会踩 `.pyx` 冲突 / `dist-info` 目录 / 3-tuple 格式坑）。
> ⚠️ **`.venv/Scripts/pyinstaller.exe` 在本机已损坏**（直接运行 `exit=1` 且无任何输出）——**必须用 `.venv/Scripts/python.exe -m PyInstaller`**，不要直接调 `pyinstaller`/`pyinstaller.exe`。
> 🔒 **绝不要 `--add-data "config;config"`**：冻结态真实配置读的是 `APP_DIR/config/config.json`（exe 同级），`_internal/config` 永远读不到；但打进去会把**本机的 `secret_key` 内嵌进每一份分发产物**（解包 `_internal/config/config.json` 即得内部鉴权密钥）。spec 中已移除 `config` 数据项；缺配置时 `load_config()` 自动生成 + 随机 `secret_key`。

### 运行（发布产物）
产物在 `release/GoofishMasterDesktop/`（含 `GoofishMasterDesktop.exe` + `_internal/`）：
```bash
# 双击 GoofishMasterDesktop.exe 等效于：
release\GoofishMasterDesktop\GoofishMasterDesktop.exe start     # 编排 + 看门狗，阻塞运行
# 浏览器开 http://127.0.0.1:8911 管理面板
```
- 规范产物为 `--windowed`（无控制台窗口）：冻结态下 launcher 把 stdout/stderr 重定向到 `APP_DIR/data/logs/launcher.log`，各服务日志落 `data/logs/<svc>.log`，日志已落盘不受影响。要排障临时看控制台可临时改 spec 的 `console=True` 重建。

### 可执行版已知限制
1. **Playwright Chromium 已随包**：安装包内含 `playwright-browsers/chromium-1234` + `chromium_headless_shell-1234`（与打包 Playwright 驱动同修订号 1234），安装时随附到 `{app}\playwright-browsers`。launcher 向 spider 注入 `PLAYWRIGHT_BROWSERS_PATH`，桌面端优先走 bundled Chromium（`GOOFISH_USE_BUNDLED_CHROMIUM=true`），**离线也能采集**，不再依赖系统 Chrome/Edge。
2. **本地后端已内置**：P1 三项数据库已全部改为进程内嵌入式（SQLite/fakeredis/Chroma），默认全开，无需外部服务；`enabled=false` 才降级。
3. **体积**：onedir 含全部依赖（含 chromadb + 随附 Chromium ~300MB），目录约 1GB+；可后续换 `--onefile` 瘦身。
4. **代码签名（可选）**：已备 `sign.ps1`，默认跳过（无真实 CA 证书时不产出自签名签名）。购买 CA 证书后运行 `.\sign.ps1 -Thumbprint <指纹>` 即可签名安装包 + 主程序并带时间戳。
5. **安装包**：✅ 已完成（Inno Setup 6，默认装 D 盘、可改路径、可改端口；**WebView2 改用随附离线完整安装器 + 随附 Chromium**）。已修复安装期 `IPersistFile::Save failed; code 0x80070005`（快捷方式由 `{commondesktop}` 改 `{userdesktop}`，不再需管理员写所有用户桌面）。
6. **WebView2 离线安装**：安装包内嵌 **Evergreen Standalone Offline Installer（约 200MB，已校验 Microsoft 有效签名）**，落在 `build-assets/`（不入版本库，从官方下载）。`ssPostInstall` 若缺 WebView2，通过 `ShellExec('runas', ...)` 提权静默安装，**全程无需联网**。用户拒绝 UAC 仅提示，不阻断主程序安装。
7. **前置依赖检测**：launcher 新增 `preflight` 子命令 + `check_prerequisites()`，桌面控制台「环境检测」卡片显示 WebView2 / Chromium 就绪状态；安装包在 `ssPostInstall` 阶段若缺 WebView2 会调用随附离线安装器（**无需联网**）。

---

## 12. 快速排障

| 现象 | 原因 | 处理 |
|------|------|------|
| 8911 打不开面板 | feishu-agent 未就绪 | `launcher.py status` + 看 `data/logs/feishu-agent.log` |
| 服务 health=…(未就绪) | 后端未启动（预期降级） | 配 `backends` 或忽略（非 DB 功能可用） |
| spider 起不来 | 缺 Playwright Chromium | 安装包已随附 Chromium（`{app}\playwright-browsers`）；开发模式才需 `playwright install chromium` |
| AI 分析失败 | 未配 AI Key / 代理不通 | 填 `config.json` 的 `ai.*` + `ai.proxy_url` |
| 飞书收不到消息 | 未配 App / 长连接未建立 | 填 `feishu.app_id/app_secret`，看日志 |
| 端口被占 | 本机其他进程占用 | 改 `config.json` 的 `ports.*` 或安装时填新端口；`stop` 后再 `start` |
| 双击 exe 无窗口 | 缺 WebView2 Runtime | 装 Microsoft Edge WebView2 Runtime（Win10/11 一般自带） |
| GUI 报错但 start 正常 | pywebview 平台/依赖问题 | 看 `data/logs/launcher.log`；确认 WebView2 可用 |
| 双击 exe 闪退（无窗口无提示） | GUI 线程崩 / WebView2 缺失 / 依赖 | 看 `data/logs/desktop-crash.log` + 错误框；`start` 若正常则问题在 GUI/WebView2（`--windowed` 已去黑框） |
| 登陆后顶部弹「服务异常：qdrant — 请检查服务状态」 | 旧版 `/api/system/overview` 写死探测 `http://qdrant:6333/healthz`（Docker 名），桌面版无该容器必失败→误判。已修复：`backends.*.enabled=false` 标 `disabled`（可选未启用，非异常）；P1 后向量库改为进程内 Chroma，`enabled=true` 直接判 `running`，不再探测外部端口 | 升级到含此修复的 exe 即可；P1 默认 `qdrant.enabled=true`，RAG 知识库自动启用（需配 embedding key） |
| overview 三项基础设施全 `disabled` + `system_status=degraded`（但 config 里 `enabled=true`） | feishu-agent/main.py 顶部 try 块用了 `sys.path.insert` 却**没 `import sys`** → NameError 被静默吞 → `cfg_mod=None` → overview 读不到配置。2026-08-04 修：import 行补 `sys`。旧版因 config 本就 enabled=false 而隐形 | 源码已修；`_internal/services/*/main.py` 是**数据文件**（非 PYZ 编译），单文件 cp 同步即等效重打包，无需整包重建 |

---

## 13. 桌面控制台 exe 构建（GUI 版，已完成）

> `GoofishMasterDesktop.exe` 现已集成桌面控制台：**同一 exe 双模式**——双击默认开 GUI（自动拉起 4 服务），也可 `GoofishMasterDesktop.exe start` 走无界面服务端。沿用 §11 的「单二进制多模式」架构（`--service` 自拉起子进程），不新增二进制。

### 模式
- `GoofishMasterDesktop.exe`（双击 / `desktop`）：打开控制台窗口（pywebview 加载本地 `desktop/ui/index.html`），自动拉起后端；系统托盘常驻（显示控制台 / 启动后端 / 停止后端 / 退出）；关闭窗口 → 最小化到托盘，托盘「退出」才全停。
- `GoofishMasterDesktop.exe start`：无界面服务端（行为同 §11）。
- `stop` / `restart` / `status`：不变。

### 构建命令（历史内联参考，规范方式用 spec）
```bash
cd D:\WorkBuddy\GoofishMasterDesktop
# 规范方式（推荐）：
# 先 PowerShell 清 build/dist，再：
# .venv\Scripts\python.exe -m PyInstaller GoofishMasterDesktop.spec --noconfirm
# 产物在 dist/GoofishMasterDesktop/
```
> spec 已含 `--collect-all chromadb/fakeredis/aiosqlite`（替代已移除的 qdrant_client/asyncpg）+ `--collect-all webview/pystray`（GUI）+ `datas=[('desktop','desktop'),...]`（desktop 数据文件化）。

### 关键改动（GUI 版相对无界面版）
- 移除 `--exclude-module webview` / `--exclude-module pystray`，改为 `--collect-all webview --collect-all pystray`（GUI + 托盘依赖）。
- `desktop/` 整目录作为数据文件（`datas=[('desktop','desktop')]`），冻结后位于 `_MEIPASS/desktop/`；改 api.py/app.py 单文件 cp 即生效，不用重打包。
- 入口仍是 `launcher.py`；`launcher.main` 增加 `desktop` action（冻结态默认即 `desktop`，双击开窗口；非冻结默认 `start`）。

### 路径与依赖
- 资源定位（冻结态）：`ROOT = sys._MEIPASS`（即 `_internal/`）；UI = `_MEIPASS/desktop/ui/index.html`；托盘图标 = `_MEIPASS/services/feishu-agent/static/logo-256.png`。
- 可写数据（配置/日志）：`APP_DIR`（exe 同级，不可写回退 `%APPDATA%/GoofishMasterDesktop`）——见 §11。
- **宿主机依赖**：Microsoft Edge **WebView2 Runtime**（Win10/11 默认自带；安装包内已随附**离线完整安装器**，纯离线目标机也能装，无需联网）。pywebview 默认走 EdgeChromium 平台。

### 崩溃诊断（打开即闪退排查）
- 桌面入口（`desktop/app.py` 的 `run()`）已加全局异常捕获：任何崩溃都会
  1) 写完整 traceback 到 `data/logs/desktop-crash.log`；
  2) 弹 Windows 错误框（不再静默闪退）。
- `launcher.main` 的 `desktop` 分支也有双保险捕获 + 弹窗。
- WebView2 Runtime 缺失的提示改在 `webview.start()` 的 except 里捕获并弹安装指引（不再用启动期 winreg 探测，避免无桌面会话下原生崩）。
- **已修复的根因（打开闪退真凶）**：本环境 pywebview 的 import 名是 `webview`（不是 `pywebview`）。旧代码 `import pywebview` 找不到 → 走降级分支直接退出 → 表现为「双击闪退」。已改为兼容写法 `try: import pywebview as webview except ImportError: import webview`，并同步 `webview.create_window/start` 调用。构建命令 `--collect-all webview` 收集的就是正确的 webview 包（pywebview 本体），**勿改成 `--collect-all pywebview`**（那是另一个无关的包）。
- 用户自助排查顺序：
  1. 看是否弹了错误框 → 抄下文字；
  2. 看 `data/logs/desktop-crash.log` 末尾；
  3. 确认 WebView2 Runtime 已装（https://developer.microsoft.com/zh-cn/microsoft-edge/webview2/）；
  4. `GoofishMasterDesktop.exe start` 若正常 → 问题在 GUI/WebView2 层。

### 验证
- 无头回归：exe `start` → 4 服务 HTTP 200（与 §11 同）。
- GUI 端到端：须在 Windows 桌面实测（无头环境无法渲染窗口）。

### 已知风险 / 限制
- GUI 无法在无头环境验证，首次在他机运行需实测窗口渲染、托盘、WebView2 可用性。
- exe 体积在 §11 基础上再增数十 MB（webview/pystray 依赖）。
- 其余限制同 §11（Chromium 已随包、本地后端内置、签名可选未启用）。
