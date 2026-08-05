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
- **WebView2 改用随附离线完整安装器（约 200MB，已内置）**：安装时**全程无需联网**。若系统缺失 WebView2，安装末尾会自动请求管理员权限静默安装（点「是」即可；点「否」也不会中断软件本身安装）
- **修复安装报错 `IPersistFile::Save failed; code 0x80070005 拒绝访问`**：旧版会在所有用户桌面建快捷方式，因非提权而在部分机器上失败；本版改为当前用户桌面，不再报错
- 默认安装到 `D:\GoofishMasterDesktop`（可改），端口安装时可自定义（默认 8911/8912/8913/8914，仅绑定 127.0.0.1 本机）

## 安装步骤

1. 下载下方 `GoofishMasterDesktop-Setup-1.0.0.exe`（约 509MB）
2. 双击运行安装包
3. 若 Windows SmartScreen 弹出「Windows 已保护你的电脑」——**正常**（见已知问题），点「更多信息」→「仍要运行」
4. 选择路径、设端口，勾选「创建桌面快捷方式」
5. 完成後桌面双击 `GoofishMasterDesktop` 启动；首次启动自动生成随机 `secret_key`

> 首次使用需在 `config.json`（或桌面控制台）填写 AI Key（DeepSeek 等）与飞书 App 信息，否则只有本地分析能力、飞书推送不可用。

## ⚠️ 已知问题 / 待反馈

- **未做代码签名**：安装包与主程序**没有数字签名**，SmartScreen 会拦截/告警。点「仍要运行」即可，不影响功能。正式签名将在购买 CA 证书后的版本加入。
- **GUI 尚未在多台真实机器实测**：如遇「双击无窗口 / 闪退」，请把 `data/logs/desktop-crash.log` 内容反馈。
- **体积较大**：因内置 Chromium + WebView2 离线器，安装包约 509MB，暂未瘦身。
- **飞书推送需自建应用**：本项目不提供飞书机器人凭证，需自己在飞书开放平台创建应用并填 `app_id / app_secret`。
- **AI 分析调用外部大模型**：需自行配置 API Key；调用境外模型（如 Gemini）可能需配置代理。

## 如何反馈

测试版最缺真实环境反馈。遇到问题请在 Issue 里贴出：操作系统版本、报错截图或 `data/logs/` 日志、复现步骤。每一条反馈都能让小工具更稳一点。

## 支持作者

如果这款小工具帮到了你，欢迎扫码随意打赏一杯咖啡或一瓶水。金额随意，心意最重要。先谢过大家！

| 微信支付 | 支付宝 |
| --- | --- |
| ![微信收款](assets/donate-wechat.png) | ![支付宝收款](assets/donate-alipay.jpg) |

---

完整发行说明见仓库 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

**校验信息**：版本 `v1.0.0-beta.1` · 安装包 `GoofishMasterDesktop-Setup-1.0.0.exe` · 约 509 MB · Windows 10/11 x64 · 许可 Private
