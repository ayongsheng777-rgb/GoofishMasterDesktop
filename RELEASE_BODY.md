# GoofishMasterDesktop 闲鱼圣手桌面端 v1.1.2

> **v1.1.2 体验完善版**——修复日志崩塌、AI 限流退避，新增多页采集参数。
> 作者是一名线下电脑实体店店主，利用 WorkBuddy 在业余时间开发，欢迎试用并提出问题。

## 这是什么

`GoofishMasterDesktop` 是「闲鱼圣手 / Goofish Master」二手商品情报系统的**桌面独立运行端**：
搜索 → AI 三维分析 → 决策打分 → 卡片推送 + 持久监控，全部在**你自己的电脑上**跑，无需服务器、无需 Docker。

- 4 个微服务（feishu-agent / ai-router / agent-pipeline / spider-service）由桌面端一键拉起
- 数据全嵌入式：SQLite（替代 PostgreSQL）、fakeredis（替代 Redis）、Chroma（替代 Qdrant）
- 桌面控制台（pywebview + 系统托盘），双击即用

## 🆕 v1.1.2 本次新增

- **修复日志崩塌**：蜘蛛爬虫的 `sys.stdout.detach()` 在采集时撕裂 uvicorn/logging 底层 buffer，导致全程日志全是 `ValueError: underlying buffer has been detached` 报错（几千行噪音覆盖真实采集进度，排查不可见）。已改为 `io.TextIOWrapper` 安全重包装。
- **AI 429 退避机制**：API 限流 / 额度不足时不再立即重试 ×4 反复撞墙，改为指数退避（2^N + 随机抖动，上限 60s），避免限流风暴。管线层已有单品异常隔离，单件分析失败不会拖死整次搜索。
- **多页采集参数**：飞书搜索指令支持追加「N页」，如 `找 笔记本电脑 3页` 可采集约 90 件商品（默认 1 页 / 约 30 件，上限 10 页）。搜索确认回复会动态显示预估耗时。全链路透传：命令解析 → 飞书代理 → 管线 → 蜘蛛。
- **使用说明文档完善**：指令帮助中新增「N页」条件筛选说明与示例。

## 安装步骤

1. 下载下方 `GoofishMasterDesktop-Setup-1.1.2.exe`（约 580MB）
2. 双击运行安装包
3. 若 Windows SmartScreen 弹出「Windows 已保护你的电脑」——正常（见已知问题），点「更多信息」→「仍要运行」
4. 选择路径、设端口，勾选「创建桌面快捷方式」
5. 完成后桌面双击 `GoofishMasterDesktop` 启动；首次启动自动生成随机 `secret_key`

> 首次使用需在 `config.json`（或桌面控制台）填写 AI Key（DeepSeek 等）与飞书 App 信息，否则只有本地分析能力、飞书推送不可用。

## ⚠️ 已知问题 / 待反馈

- **未做代码签名**：安装包与主程序没有数字签名，SmartScreen 会拦截/告警。点「仍要运行」即可，不影响功能。正式签名将在购买 CA 证书后的版本加入。
- **体积较大**：因内置 Chromium + 固定版 WebView2 运行时，安装包约 580MB，暂未瘦身。
- **首次使用必须先登录闲鱼**：搜索/监控依赖登录态，请先在管理后台「🐟 闲鱼登录」扫码（飞书已配置时也可直接发「闲鱼登录」指令）。
- **飞书推送需自建应用**：本项目不提供飞书机器人凭证，需自己在飞书开放平台创建应用并填 `app_id / app_secret`（也可扫码后在 App 内一键新建 / 选择机器人绑定）。
- **AI 分析调用外部大模型**：需自行配置 API Key；调用境外模型（如 Gemini / OpenAI）可能需配置代理。

## 如何反馈

遇到问题请在 Issue 里贴出：操作系统版本、报错截图或 `data/logs/` 日志、复现步骤。每一条反馈都能让小工具更稳一点。

## 支持作者

如果这款小工具帮到了你，欢迎扫码随意打赏一杯咖啡或一瓶水。金额随意，心意最重要。先谢过大家！

| 微信支付 | 支付宝 |
| --- | --- |
| ![微信收款](https://github.com/ayongsheng777-rgb/GoofishMasterDesktop/releases/download/v1.1.2/donate-wechat.png) | ![支付宝收款](https://github.com/ayongsheng777-rgb/GoofishMasterDesktop/releases/download/v1.1.2/donate-alipay.jpg) |

---

## 致谢

本项目参考并借用了 [Usagi-org/ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor)（闲鱼智能监控系统，MIT 许可证）的部分代码与设计思路，特此注明。特别感谢原作者 **Usagi** 的开源贡献——这是一个基于 Playwright + 多模态 AI 的闲鱼实时监控与分析系统，后台 UI 完善、功能扎实，是同类项目里做得相当出色的一个；本桌面端的本地化思路也从中受益良多。郑重致谢，也推荐有 Docker 条件的朋友去原项目点 Star ⭐ 支持。

---

## 免责声明

本软件仅供个人学习、技术研究与非商业用途。使用者须遵守闲鱼等相关平台的服务条款与所在地法律法规，自行控制访问频率与规模、不得从事违规或侵权行为。作者不对使用后果承担任何责任，软件按「现状」提供、不作任何担保。完整条款见仓库 [DISCLAIMER.md](DISCLAIMER.md)。

---

完整发行说明见仓库 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

**校验信息**：版本 `v1.1.2`（2026-08-06 发布）· 安装包 `GoofishMasterDesktop-Setup-1.1.2.exe` · 约 580 MB（580,497,499 字节）· SHA-256 `da0080a2a945942a0bce7986d04fa034734ea9fc404fd946dcc0939f78d38f29` · Windows 10/11 x64 · 许可 MIT
