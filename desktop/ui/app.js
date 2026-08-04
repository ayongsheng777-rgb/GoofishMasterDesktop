// GoofishMasterDesktop 桌面控制台 · 前端逻辑
// 通过 pywebview 桥接 window.pywebview.api 与 Python 端交互。

const NAME_MAP = {
  "feishu-agent": "飞书智能体",
  "ai-router": "AI 路由",
  "agent-pipeline": "分析编排",
  "spider-service": "采集服务",
};

const POLL_MS = 3000;
let api = null;
let statusTimer = null;

function whenReady(cb) {
  if (window.pywebview && window.pywebview.api) {
    api = window.pywebview.api;
    cb();
  } else {
    window.addEventListener("pywebviewready", () => {
      api = window.pywebview.api;
      cb();
    });
  }
}

function el(id) { return document.getElementById(id); }

// ---------- 状态渲染 ----------
function renderStatus(list) {
  const grid = el("serviceGrid");
  const running = list.filter((s) => s.running && s.health).length;
  const total = list.length;

  // 汇总药丸
  const pill = el("summaryPill");
  pill.className = "pill " + (running === total ? "pill-ok" : running === 0 ? "pill-bad" : "pill-warn");
  pill.textContent = `${running}/${total} 服务运行中`;

  // 全局按钮
  const btn = el("toggleBtn");
  const anyUp = list.some((s) => s.running);
  btn.textContent = anyUp ? "全部停止" : "全部启动";
  btn.className = "btn " + (anyUp ? "btn-danger" : "btn-primary");

  grid.innerHTML = "";
  list.forEach((s) => {
    const healthy = s.running && s.health;
    const starting = s.running && !s.health;
    const dotCls = healthy ? "dot-ok" : starting ? "dot-warn" : "dot-bad";
    const stateTxt = healthy ? "健康运行" : starting ? "启动中…" : "已停止";
    const pidTxt = s.pid ? `PID ${s.pid}` : "未运行";

    const card = document.createElement("div");
    card.className = "svc-card";
    card.innerHTML = `
      <div class="svc-top">
        <div class="svc-name">${NAME_MAP[s.name] || s.name}</div>
        <div class="svc-port">:${s.port}</div>
      </div>
      <div class="svc-status">
        <span class="dot ${dotCls}"></span>
        <span class="svc-state">${stateTxt}</span>
      </div>
      <div class="svc-meta">
        <span class="pid">${pidTxt}</span>
      </div>
      <div class="svc-foot">
        <button class="btn btn-ghost btn-sm" data-restart="${s.name}">重启</button>
      </div>`;
    grid.appendChild(card);
  });

  grid.querySelectorAll("[data-restart]").forEach((b) => {
    b.addEventListener("click", () => {
      const name = b.getAttribute("data-restart");
      b.disabled = true;
      b.textContent = "重启中…";
      api.restart_service(name).then(() => { refreshStatus(); refreshLog(); });
    });
  });
}

function refreshStatus() {
  if (!api) return;
  api.get_status().then(renderStatus).catch((e) => console.error(e));
}

// ---------- 日志 ----------
function refreshLog() {
  if (!api) return;
  const target = el("logTarget").value;
  api.get_logs(target, 300).then((lines) => {
    const box = el("logBox");
    box.textContent = lines.length ? lines.join("\n") : "（暂无日志）";
    box.scrollTop = box.scrollHeight;
  }).catch((e) => { el("logBox").textContent = "读取日志失败: " + e; });
}

// ---------- 配置 ----------
function renderConfig(cfg) {
  const box = el("configBox");
  const ports = cfg.ports || {};
  const items = [
    { label: "飞书智能体端口", value: ":" + (ports.feishu_agent || "-") },
    { label: "AI 路由端口", value: ":" + (ports.ai_router || "-") },
    { label: "分析编排端口", value: ":" + (ports.agent_pipeline || "-") },
    { label: "采集服务端口", value: ":" + (ports.spider || "-") },
    { label: "本地后端", value: cfg.backends_enabled ? "已启用" : "未启用（服务降级）", ok: cfg.backends_enabled },
    { label: "DeepSeek", value: tag(cfg.ai_configured && cfg.ai_configured.deepseek_api_key), raw: true },
    { label: "Gemini", value: tag(cfg.ai_configured && cfg.ai_configured.gemini_api_key), raw: true },
    { label: "Qwen", value: tag(cfg.ai_configured && cfg.ai_configured.qwen_api_key), raw: true },
    { label: "飞书凭证", value: tag(cfg.feishu_configured), raw: true },
  ];
  box.innerHTML = "";
  items.forEach((it) => {
    const d = document.createElement("div");
    d.className = "cfg-item";
    let v = it.value;
    if (it.raw) {
      v = `<span class="tag ${it.value === "已配置" ? "tag-ok" : "tag-no"}">${it.value}</span>`;
    } else if (it.ok !== undefined) {
      v = `<span class="tag ${it.ok ? "tag-ok" : "tag-no"}">${it.value}</span>`;
    }
    d.innerHTML = `<div class="cfg-label">${it.label}</div><div class="cfg-value">${v}</div>`;
    box.appendChild(d);
  });
}

function tag(b) { return b ? "已配置" : "未配置"; }

function refreshConfig() {
  if (!api) return;
  api.get_config().then(renderConfig).catch((e) => console.error(e));
}

// ---------- 环境检测 ----------
function renderPreflight(p) {
  const box = el("preflightBox");
  if (!box) return;
  const items = [
    { label: "WebView2 Runtime", ok: p.webview2_installed, msg: p.webview2_message },
    { label: "随附 Chromium", ok: p.chromium_installed, msg: p.chromium_message },
  ];
  box.innerHTML = "";
  items.forEach((it) => {
    const d = document.createElement("div");
    d.className = "env-item " + (it.ok ? "env-ok" : "env-no");
    d.innerHTML = `
      <div class="env-head">
        <span class="dot ${it.ok ? "dot-ok" : "dot-bad"}"></span>
        <span class="env-label">${it.label}</span>
        <span class="env-tag">${it.ok ? "已就绪" : "缺失"}</span>
      </div>
      <div class="env-msg">${it.msg}</div>`;
    box.appendChild(d);
  });
}

function refreshPreflight() {
  if (!api) return;
  api.check_prerequisites().then(renderPreflight).catch((e) => console.error(e));
}

// ---------- 事件绑定 ----------
function bindEvents() {
  el("toggleBtn").addEventListener("click", () => {
    const btn = el("toggleBtn");
    const stopping = btn.textContent.includes("停止");
    btn.disabled = true;
    const p = stopping ? api.stop_all() : api.start_all();
    p.then(() => { refreshStatus(); refreshLog(); btn.disabled = false; });
  });

  el("refreshLog").addEventListener("click", refreshLog);
  el("logTarget").addEventListener("change", refreshLog);
  el("openData").addEventListener("click", () => api.open_data_dir());
  el("openFrontend").addEventListener("click", () => api.open_frontend());
  el("aboutBtn").addEventListener("click", () => el("aboutModal").classList.remove("hidden"));
}

// ---------- 启动 ----------
whenReady(() => {
  bindEvents();
  refreshStatus();
  refreshLog();
  refreshConfig();
  refreshPreflight();

  statusTimer = setInterval(() => {
    refreshStatus();
    if (el("autoLog").checked) refreshLog();
  }, POLL_MS);
});
