# GoofishMasterDesktop 闲鱼圣手桌面端 v1.0.0-beta.1（测试版）

> ⚠️ **测试版（Beta）发布**，仅供尝鲜与反馈，不代表最终稳定质量。
> 作者是一名线下电脑实体店店主，利用 WorkBuddy 在业余时间开发，欢迎试用并提出问题。

## 这是什么

`GoofishMasterDesktop` 是「闲鱼圣手 / Goofish Master」二手商品情报系统的**桌面独立运行端**：
搜索 → AI 三维分析 → 决策打分 → 卡片推送 + 持久监控，全部在**你自己的电脑上**跑，无需服务器、无需 Docker。

- 4 个微服务（feishu-agent / ai-router / agent-pipeline / spider-service）由桌面端一键拉起
- 数据全嵌入式：SQLite（替代 PostgreSQL）、fakeredis（替代 Redis）、Chroma（替代 Qdrant）
- 桌面控制台（pywebview + 系统托盘），双击即用

## 本次测试版包含

### ✨ 核心能力
- **闲鱼商品监控**：关键词搜索、AI 智能分析、风险/捡漏评分、飞书卡片推送（需自建飞书应用）
- **完全离线采集**：已随安装包附带 Playwright Chromium（rev 1234），采集不依赖系统 Chrome/Edge
- **桌面控制台**：可视化查看服务状态、配置、日志；系统托盘常驻
- **环境检测卡片**：直观显示 WebView2 Runtime 与 Chromium 是否就绪

### 🔧 安装与依赖（本版重点改进）
- **WebView2 固定版本运行时随包分发（约 500MB，已内置）**：桌面窗口渲染完全离线、免 UAC、不依赖系统预装 Runtime
- **修复安装报错 `IPersistFile::Save failed; code 0x80070005 拒绝访问`**：旧版会在所有用户桌面建快捷方式，因非提权而在部分机器上失败；本版改为当前用户桌面，不再报错
- 默认安装到 `D:\GoofishMasterDesktop`（可改），端口安装时可自定义（默认 8911/8912/8913/8914，仅绑定 127.0.0.1 本机）

### 🐞 实测修复（2026-08-05 刷新，真机验证通过）
- 监控任务无法创建 / 一直无反馈（SQLite 持久化误关 + 列表入库失败）
- 搜索重启后任务中心消失；搜索时区崩溃（Windows 缺 Asia/Shanghai 数据）
- 采集失败被误报为「未找到商品」（未登录明确提示扫码，异常如实报原因）
- 任务中心「已发现数量」恒为 0、飞书「停止/删除/设置」找不到任务（PG→SQLite 参数绑定错位，系统性修复）
- 模型配置重启丢失 / 扫码二次点击 / 验证器旧名 / 桌面图标崩溃等一并修复

## 安装步骤

1. 下载下方 `GoofishMasterDesktop-Setup-1.0.0.exe`（约 580MB）
2. 双击运行安装包
3. 若 Windows SmartScreen 弹出「Windows 已保护你的电脑」——**正常**（见已知问题），点「更多信息」→「仍要运行」
4. 选择路径、设端口，勾选「创建桌面快捷方式」
5. 完成後桌面双击 `GoofishMasterDesktop` 启动；首次启动自动生成随机 `secret_key`

> 首次使用需在 `config.json`（或桌面控制台）填写 AI Key（DeepSeek 等）与飞书 App 信息，否则只有本地分析能力、飞书推送不可用。

## ⚠️ 已知问题 / 待反馈

- **未做代码签名**：安装包与主程序**没有数字签名**，SmartScreen 会拦截/告警。点「仍要运行」即可，不影响功能。正式签名将在购买 CA 证书后的版本加入。
- **体积较大**：因内置 Chromium + 固定版 WebView2 运行时，安装包约 580MB，暂未瘦身。
- **首次使用必须先登录闲鱼**：搜索/监控依赖登录态，请先在管理后台「🐟 闲鱼登录」扫码。
- **飞书推送需自建应用**：本项目不提供飞书机器人凭证，需自己在飞书开放平台创建应用并填 `app_id / app_secret`。
- **AI 分析调用外部大模型**：需自行配置 API Key；调用境外模型（如 Gemini）可能需配置代理。

## 如何反馈

测试版最缺真实环境反馈。遇到问题请在 Issue 里贴出：操作系统版本、报错截图或 `data/logs/` 日志、复现步骤。每一条反馈都能让小工具更稳一点。

## 支持作者

如果这款小工具帮到了你，欢迎扫码随意打赏一杯咖啡或一瓶水。金额随意，心意最重要。先谢过大家！

| 微信支付 | 支付宝 |
| --- | --- |
| ![微信收款](https://github.com/ayongsheng777-rgb/GoofishMasterDesktop/releases/download/v1.0.0-beta.1/donate-wechat.png) | ![支付宝收款](https://github.com/ayongsheng777-rgb/GoofishMasterDesktop/releases/download/v1.0.0-beta.1/donate-alipay.jpg) |

---

## 致谢

本项目参考并借用了 [Usagi-org/ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor)（闲鱼智能监控系统，MIT 许可证）的部分代码与设计思路，特此注明。特别感谢原作者 **Usagi** 的开源贡献——这是一个基于 Playwright + 多模态 AI 的闲鱼实时监控与分析系统，后台 UI 完善、功能扎实，是同类项目里做得相当出色的一个；本桌面端的本地化思路也从中受益良多。郑重致谢，也推荐有 Docker 条件的朋友去原项目点 Star ⭐ 支持。

---

## 免责声明

本软件仅供个人学习、技术研究与非商业用途。使用者须遵守闲鱼等相关平台的服务条款与所在地法律法规，自行控制访问频率与规模、不得从事违规或侵权行为。作者不对使用后果承担任何责任，软件按「现状」提供、不作任何担保。完整条款见仓库 [DISCLAIMER.md](DISCLAIMER.md)。

---

完整发行说明见仓库 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

**校验信息**：版本 `v1.0.0-beta.1`（2026-08-05 刷新）· 安装包 `GoofishMasterDesktop-Setup-1.0.0.exe` · 约 580 MB · SHA-256 `63f85e6685ece9c81a4b6101aa221d1d2e57b5bf9c0ab4df26a771ee4d1e30b9` · Windows 10/11 x64 · 许可 MIT
