"use strict";
const $ = id => document.getElementById(id);
let csrf = "", overview = null, pendingConfirm = null, page = "dashboard", busy = false;
let accountMode = "browser", oauthFlow = null, oauthTimer = null, oauthGeneration = 0;
const labels = {
  dashboard: ["让每个账号，各尽其用", "WORKBUDDY WORKSPACE", "在这里查看服务运行、账号积分和请求表现。"],
  accounts: ["账号池", "ACCOUNT POOL", "集中管理登录凭据、积分与可用状态，让请求自动分配到可用账号。"],
  logs: ["请求日志", "REQUEST LOG", "请求记录按天落盘，重启不清零；只记录元数据，不含提示词与回复。"],
  usage: ["用量统计", "USAGE ANALYTICS", "按日聚合积分与 token 消耗，可按账号与密钥筛选，并查看模型用量排行。"],
  keys: ["API 密钥", "CLIENT ACCESS", "为每个客户端分配独立密钥，让连接清晰可控。"],
  test: ["连接测试", "CONNECTION LAB", "从当前账号发起请求，确认模型能否正常响应。"],
  guide: ["接入指南", "GET CONNECTED", "从导入凭据到客户端接入，只需几步。"]
};
const outcomes = {success:"完成", stream_error:"流式错误", interrupted:"未完整结束", http_error:"请求失败"};
const outcomeOptions = [["", "全部结果"], ["success", "完成"], ["stream_error", "流式错误"], ["interrupted", "未完整结束"], ["http_error", "请求失败"]];
const fmtNum = (value, digits = 2) => Number(value).toLocaleString("zh-CN", {maximumFractionDigits: digits});
// 「最近请求」与「请求日志」共用同一行模板，保证两处外观完全一致。
function requestRow(r) {
  const site = r.site === "intl" ? "国际站" : r.site === "cn" ? "国内站" : "";
  const account = r.account ? `${esc(r.account)}${site ? `<small class="cell-note">${site}</small>` : ""}` : '<span class="muted">—</span>';
  return `<tr><td class="mono muted">${esc(stamp(r.time*1000))}</td><td>${r.source === "test" ? "后台测试" : "API"}<small class="cell-note mono">${esc(r.path)}</small></td><td class="mono">${esc(r.key || "—")}</td><td class="mono">${esc(r.model || "—")}</td><td>${account}</td><td><span class="pill ${r.ok ? "green" : "red"}">${r.status ?? "—"}</span><small class="cell-note">${outcomes[r.outcome] || "请求失败"}</small></td><td class="align-right mono">${r.credits === null || r.credits === undefined ? "—" : fmtNum(r.credits)}</td><td class="align-right mono">${fmtNum(r.duration_ms)} ms</td></tr>`;
}
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const stamp = ts => ts ? new Date(ts).toLocaleString("zh-CN", {hour12:false}) : "未提供";
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $("toast").hidden = true, 4500); }
function showLogin() {
  csrf = ""; overview = null; $("workspace").hidden = true; $("login").hidden = false; $("boot").hidden = true;
  document.querySelectorAll("dialog[open]").forEach(d => d.close()); $("admin-key").value = "";
}
async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = {"Content-Type":"application/json", ...(method !== "GET" ? {"X-CSRF-Token":csrf} : {})};
  const response = await fetch("/admin/api/" + path, {method, headers, credentials:"same-origin", body:options.body === undefined ? undefined : JSON.stringify(options.body)});
  let data; try { data = await response.json(); } catch { throw new Error("服务器暂时不可用，请稍后重试"); }
  if (!response.ok) {
    if (response.status === 401 && path !== "login") showLogin();
    throw new Error(typeof data.detail === "string" ? data.detail : "操作失败，请检查输入后重试");
  }
  return data;
}
function goPage(next) {
  if (!(next in labels)) next = "dashboard";
  page = next;
  document.querySelectorAll(".page-panel").forEach(el => el.hidden = el.id !== "page-" + next);
  document.querySelectorAll("[data-page]").forEach(el => { el.classList.toggle("selected", el.dataset.page === next); el.setAttribute("aria-current", el.dataset.page === next ? "page" : "false"); });
  const [title, kicker, desc] = labels[next];
  $("page-title").replaceChildren(document.createTextNode(title));
  const dot = document.createElement("span"); dot.className = "title-dot"; dot.textContent = "."; $("page-title").append(dot);
  $("breadcrumb").textContent = next === "dashboard" ? "概览" : title; $("page-kicker").textContent = kicker; $("page-desc").textContent = desc;
  history.replaceState(null, "", "#" + next);
  if (next === "logs") { loadLogs(true); loadServiceLog(); }
  if (next === "usage") loadUsage();
}
function render() {
  const {accounts, keys, models, uptime, events} = overview;
  const active = accounts.find(a => a.active && a.enabled);
  renderDashboard();
  $("account-count").textContent = accounts.length; $("account-badge").textContent = accounts.length;
  const rotating = overview.pool?.routing === "round_robin";
  $("active-name").textContent = rotating ? "账号池轮转" : active?.name || "未选择账号";
  $("active-state").textContent = rotating ? "新请求轮流分配，跳过暂停与冷却账号" : "新请求使用手动指定账号";
  $("test-account").textContent = active?.name || "尚未选择";
  $("uptime").textContent = uptime < 60 ? "已启动不到 1 分钟" : `持续运行 ${Math.floor(uptime / 3600)} 小时 ${Math.floor(uptime % 3600 / 60)} 分钟`;
  $("accounts-empty").hidden = accounts.length > 0;
  renderAccounts();
  const count = status => accounts.filter(a => a.pool_state === status).length;
  $("pool-counts").textContent = `全部 ${accounts.length}  ·  可用 ${count("available")}  ·  冷却 ${count("cooling")}  ·  暂停 ${count("paused")}  ·  耗尽 ${count("exhausted")}`;
  const known = accounts.filter(a => a.remaining !== null && a.remaining !== undefined);
  $("total-credits").textContent = known.length ? known.reduce((s,a) => s + a.remaining, 0).toLocaleString("zh-CN", {maximumFractionDigits:2}) + (known.length < accounts.length ? "（部分）" : "") : "待查询";
  if (!$("pool-settings").contains(document.activeElement)) {
    $("pool-routing").value = overview.pool?.routing || "manual"; $("auto-checkin").checked = overview.pool?.auto_checkin || false; $("checkin-time").value = overview.pool?.checkin_time || "09:00";
  }
  $("keys-body").innerHTML = keys.map(k => `<tr><td><strong>${esc(k.name)}</strong></td><td><code>${esc(k.hint)}</code></td><td class="muted mono">${esc(stamp(k.created * 1000))}</td><td class="align-right"><div class="actions"><button class="danger" data-revoke="${k.id}">撤销</button></div></td></tr>`).join("");
  const selected = $("model").value || "deepseek-v4-flash";
  $("model").replaceChildren(...models.map(model => { const o = document.createElement("option"); o.value = o.textContent = model; return o; }));
  if (models.includes(selected)) $("model").value = selected;
  $("test-submit").disabled = !active || busy;
  $("test-history").innerHTML = events.length ? events.map(e => `<div class="history-row"><span class="mono muted">${esc(stamp(e.time * 1000))}</span><strong>${esc(e.model)}</strong><span class="pill ${e.ok ? "green" : "red"}">${e.ok ? "成功" : "失败"}</span><span class="mono">${e.seconds}s</span></div>`).join("") : '<p class="history-empty">暂无测试记录，发送第一条测试消息。</p>';
}
function renderDashboard() {
  const {accounts, metrics:m = {}, pool} = overview;
  const fmt = value => Number(value).toLocaleString("zh-CN",{maximumFractionDigits:2});
  const rate = value => value === null || value === undefined ? "—" : value.toFixed(1) + "%";
  $("dash-available").textContent = accounts.filter(a=>a.pool_state === "available").length + " / " + accounts.length;
  const known = accounts.filter(a=>typeof a.remaining === "number");
  $("dash-credits").textContent = known.length ? fmt(known.reduce((s,a)=>s+a.remaining,0)) : "—";
  $("dash-credit-note").textContent = known.length < accounts.length ? `已查询 ${known.length}/${accounts.length} 个账号，余额可能不完整` : accounts.some(a=>a.credits_stale) ? "包含待刷新余额，以最近一次查询为准" : "以最近一次上游查询为准";
  $("dash-requests").textContent = fmt(m.completed || 0);
  $("dash-request-note").textContent = `API ${m.api_count || 0} · 后台测试 ${m.test_count || 0}`;
  $("dash-success").textContent = rate(m.success_rate); $("dash-http").textContent = rate(m.http_success_rate);
  $("dash-failed").textContent = m.failed || 0; $("dash-inflight").textContent = m.in_flight || 0;
  $("dash-latency").textContent = m.avg_duration_ms === null || m.avg_duration_ms === undefined ? "—" : fmt(m.avg_duration_ms) + " ms";
  $("dash-since").textContent = "统计开始于 " + stamp((m.started_at || 0)*1000);
  $("dash-routing").textContent = pool?.routing === "round_robin" ? "轮流分配请求，自动跳过不可用账号" : "手动指定账号模式";
  const states={available:"可用",paused:"已暂停",cooling:"冷却中",exhausted:"积分耗尽",invalid:"凭据异常"};
  $("dash-account-list").innerHTML = accounts.slice(0,8).map(a=>`<div class="dashboard-account"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}</strong><small class="cell-note">${esc(a.uid || a.nickname)}</small></div><div class="dashboard-account-credit"><strong>${a.remaining === null || a.remaining === undefined ? "待查询" : fmt(a.remaining)}</strong><small class="cell-note">积分</small></div><span class="pill ${a.pool_state === 'available' ? 'green' : 'amber'}">${states[a.pool_state] || '待查询'}</span></div>`).join("") || '<p class="history-empty">尚未添加账号，点击“添加账号”开始。</p>';
  if(accounts.length > 8) $("dash-account-list").insertAdjacentHTML("beforeend",'<p class="muted">更多账号请前往账号池查看。</p>');
  $("dash-recent-body").innerHTML=(m.recent || []).map(requestRow).join("") || '<tr><td colspan="8" class="history-empty">尚无请求记录。发起 API 调用或后台测试后，这里会自动更新。</td></tr>';
}
document.querySelectorAll("[data-dashboard-page]").forEach(b=>b.addEventListener("click",()=>goPage(b.dataset.dashboardPage)));
$("dash-add").addEventListener("click",()=>openAccount());
$("dash-base").textContent=location.origin+"/v1";
$("dash-copy").addEventListener("click",()=>copy(location.origin+"/v1"));
function renderAccounts() {
  const query = $("account-search").value.toLowerCase(), filter = $("account-filter").value;
  const rows = overview.accounts.filter(a => (!query || [a.name,a.nickname,a.uid].join(" ").toLowerCase().includes(query)) && (filter === "all" || a.pool_state === filter));
  const labels = {available:["可用","green"],cooling:["冷却中","amber"],paused:["已暂停",""],exhausted:["积分耗尽","amber"],invalid:["凭据异常","red"]};
  $("accounts-body").innerHTML = rows.map(a => {
    const status = labels[a.pool_state] || ["待查询", ""];
    const credits = a.remaining === null || a.remaining === undefined ? "—" : Number(a.remaining).toLocaleString("zh-CN",{maximumFractionDigits:2});
    const site = a.site === "intl" ? '<span class="mini-active">国际站</span>' : (a.site ? '<span class="mini-active">国内站</span>' : "");
    return `<tr><td><div class="account-cell"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}${site}${a.active ? '<span class="mini-active">手动 / 测试账号</span>' : ""}</strong><small>${esc(a.uid || a.nickname)}</small></div></div></td><td><span class="pill ${status[1]}">${status[0]}</span><small class="cell-note">${a.today_checked_in ? "今日已签到" : "今日未确认签到"}</small>${a.cooldown_until > Date.now()/1000 ? `<small class="cell-note">至 ${esc(stamp(a.cooldown_until*1000))}</small>` : ""}</td><td><strong class="credit-number">${credits}</strong><small class="cell-note">${a.credits_updated ? esc(stamp(a.credits_updated*1000)) : "点击查询积分"}${a.credits_stale && a.credits_updated ? " · 待刷新" : ""}</small>${a.last_error ? `<small class="cell-note field-error">${esc(a.last_error)}</small>` : ""}</td><td class="mono">${esc(stamp(a.expires_at))}<small class="cell-note">${a.expired ? "已到期 · 调用时尝试刷新" : "支持自动刷新"}</small></td><td><div class="actions pool-actions">${a.enabled ? `<button data-action="status" data-id="${a.id}" title="查询积分与签到状态">查询积分</button><button data-action="checkin" data-id="${a.id}">签到</button><button data-action="refresh" data-id="${a.id}">刷新凭据</button>` : ""}${a.enabled && !a.active ? `<button class="switch" data-action="activate" data-id="${a.id}">设为手动 / 测试</button>` : ""}<button data-action="rename" data-id="${a.id}">备注</button><button data-action="toggle" data-id="${a.id}">${a.enabled ? "暂停" : "恢复"}</button><button class="danger" data-action="delete" data-id="${a.id}">删除</button></div></td></tr>`;
  }).join("") || (overview.accounts.length ? '<tr><td colspan="5" class="muted">没有符合筛选条件的账号。</td></tr>' : "");
  renderModelCost();
}

function renderModelCost() {
  const table = overview.model_cost || {};
  const rows = Object.keys(table).sort().map(model => {
    const entry = table[model] || {};
    const cell = site => {
      const verdict = entry[site];
      if (verdict === "free") return '<span class="pill green">免费</span>';
      if (verdict === "paid") return '<span class="pill amber">收费</span>';
      return '<span class="muted">未实测</span>';
    };
    const preferred = entry.cn === "free" && entry.intl === "paid" ? "国内站" : entry.intl === "free" && entry.cn === "paid" ? "国际站" : "—";
    return `<tr><td class="mono">${esc(model)}</td><td>${cell("cn")}</td><td>${cell("intl")}</td><td>${preferred === "—" ? '<span class="muted">不启用优先</span>' : `优先 ${preferred}`}</td></tr>`;
  }).join("");
  $("model-cost-body").innerHTML = rows || '<tr><td colspan="4" class="history-empty">尚无实测记录。发一次请求后，这里会记录该模型在两个站点的实际计费。</td></tr>';
  $("model-cost-count").textContent = Object.keys(table).length;
  renderThreshold();
}
function thresholdValue() {
  const value = Number($("min-credits").value);
  return Number.isFinite(value) && value >= 0 ? value : null;
}
function renderThreshold() {
  // 用户正在输入时不要用服务端值覆盖他
  if (document.activeElement !== $("min-credits")) {
    $("min-credits").value = overview.pool?.min_credits ?? 50;
  }
  const threshold = thresholdValue();
  const accounts = overview.accounts || [];
  const low = accounts.filter(a => a.enabled && typeof a.remaining === "number" && threshold !== null && a.remaining <= threshold);
  const unknown = accounts.filter(a => a.enabled && (a.remaining === null || a.remaining === undefined));
  const parts = [];
  if (threshold === null) {
    parts.push("请输入 0 或更大的数字。");
  } else if (threshold === 0) {
    parts.push("当前已关闭低余额保护，所有账号都会承接付费模型。");
  } else {
    parts.push(`余额不超过 ${fmtNum(threshold)} 的账号有 ${low.length} 个${low.length ? `（${low.map(a => a.name).join("、")}）` : ""}，它们不再承接已实测收费的模型；免费模型与尚未实测的模型不受影响。`);
  }
  if (unknown.length) parts.push(`另有 ${unknown.length} 个账号余额未知（${unknown.map(a => a.name).join("、")}），暂不拦截。`);
  $("min-credits-note").textContent = parts.join(" ");
}
async function saveThreshold() {
  const threshold = thresholdValue();
  if (threshold === null) { toast("阈值需为 0 或更大的数字"); $("min-credits").value = overview.pool?.min_credits ?? 50; renderThreshold(); return; }
  try {
    await api("pool/settings", {method: "PATCH", body: {min_credits: threshold}});
    overview.pool = {...(overview.pool || {}), min_credits: threshold};
    toast(threshold === 0 ? "已关闭低余额保护" : `低余额保护阈值已设为 ${fmtNum(threshold)}`);
  } catch (error) { toast(error.message); $("min-credits").value = overview.pool?.min_credits ?? 50; }
  renderThreshold();
}
$("min-credits").addEventListener("change", saveThreshold);

const logState = {limit: 100, records: [], hasMore: false, model: "", key: "", account: "", outcome: "", usage: null, loading: false};
function logQuery() {
  const params = new URLSearchParams({limit: logState.limit, offset: logState.records.length});
  if (logState.model) params.set("model", logState.model);
  if (logState.key) params.set("key", logState.key);
  if (logState.account) params.set("account", logState.account);
  if (logState.outcome) params.set("outcome", logState.outcome);
  return params;
}
async function loadLogs(reset) {
  if (logState.loading) return;
  logState.loading = true;
  if (reset) logState.records = [];
  $("logs-more").disabled = true;
  try {
    const data = await api("logs?" + logQuery().toString());
    logState.records = logState.records.concat(data.records || []);
    logState.hasMore = !!data.has_more;
    logState.usage = data.usage || null;
    renderLogs();
  } catch (error) { toast(error.message); }
  finally { logState.loading = false; $("logs-more").disabled = !logState.hasMore; }
}
function renderLogs() {
  const rows = logState.records.map(requestRow).join("");
  $("logs-body").innerHTML = rows || `<tr><td colspan="8" class="history-empty">${logState.model || logState.key || logState.account || logState.outcome ? "没有符合筛选条件的记录。" : "尚无请求记录。"}</td></tr>`;
  $("logs-count").textContent = logState.records.length;
  $("logs-more").hidden = !logState.hasMore;
  $("logs-more").disabled = !logState.hasMore;
  const usage = logState.usage;
  $("logs-usage").textContent = usage
    ? `已加载 ${logState.records.length} 条 · 日志目录共 ${usage.files} 个文件、${(usage.bytes/1048576).toFixed(2)} MB · 请求日志覆盖 ${usage.request_days} 天 · 日志不会自动删除，请自行清理`
    : "正在读取日志目录…";
  $("logs-dir").textContent = usage ? usage.dir : "";
}
function logsFilterChanged() {
  logState.model = $("logs-model").value.trim();
  logState.key = $("logs-key").value.trim();
  logState.account = $("logs-account").value.trim();
  logState.outcome = $("logs-outcome").value;
  loadLogs(true);
}
$("logs-outcome").replaceChildren(...outcomeOptions.map(([value, text]) => { const option = document.createElement("option"); option.value = value; option.textContent = text; return option; }));
["logs-model", "logs-key", "logs-account"].forEach(id => $(id).addEventListener("change", logsFilterChanged));
$("logs-outcome").addEventListener("change", logsFilterChanged);
$("logs-refresh").addEventListener("click", () => loadLogs(true));
$("logs-clear").addEventListener("click", () => {
  $("logs-model").value = ""; $("logs-key").value = ""; $("logs-account").value = ""; $("logs-outcome").value = "";
  logsFilterChanged();
});
$("logs-more").addEventListener("click", () => loadLogs(false));

const levelPill = {WARNING: "amber", ERROR: "red", CRITICAL: "red"};
const serviceState = {level: "", logger: "", entries: [], counts: {}, loggers: []};
const SERVICE_ORDER = ["ERROR", "CRITICAL", "WARNING", "INFO", "DEBUG", "OTHER"];
function renderServiceLevels() {
  const counts = serviceState.counts || {};
  const total = Object.values(counts).reduce((sum, n) => sum + n, 0);
  const options = [["", `全部级别（${total}）`]];
  SERVICE_ORDER.forEach(level => { if (counts[level]) options.push([level, `${level}（${counts[level]}）`]); });
  $("service-level").replaceChildren(...options.map(([value, text]) => { const option = document.createElement("option"); option.value = value; option.textContent = text; return option; }));
  $("service-level").value = serviceState.level;
  const loggerOptions = [["", "全部来源"]].concat((serviceState.loggers || []).map(item => [item.name, `${item.name}（${item.count}）`]));
  $("service-logger").replaceChildren(...loggerOptions.map(([value, text]) => { const option = document.createElement("option"); option.value = value; option.textContent = text; return option; }));
  $("service-logger").value = serviceState.logger;
}
function renderServiceLog() {
  $("service-body").innerHTML = serviceState.entries.map(e => {
    const detail = e.detail ? `<details class="log-detail"><summary>展开 ${e.detail.split("\n").length} 行详情</summary><pre>${esc(e.detail)}</pre></details>` : "";
    return `<tr><td class="mono muted">${esc(e.time || "—")}</td><td>${e.level ? `<span class="pill ${levelPill[e.level] || ""}">${esc(e.level)}</span>` : '<span class="muted">—</span>'}</td><td class="mono">${esc(e.logger || "—")}</td><td>${esc(e.message)}${detail}</td></tr>`;
  }).join("") || '<tr><td colspan="4" class="history-empty">暂无服务日志。服务启动并产生输出后，这里会自动记录。</td></tr>';
  $("service-count").textContent = serviceState.entries.length;
}
async function loadServiceLog() {
  try {
    const params = new URLSearchParams({lines: 300});
    if (serviceState.level) params.set("level", serviceState.level);
    if (serviceState.logger) params.set("logger", serviceState.logger);
    const data = await api("logs/service?" + params.toString());
    serviceState.entries = data.entries || [];
    serviceState.counts = data.counts || {};
    serviceState.loggers = data.loggers || [];
    renderServiceLevels();
    renderServiceLog();
  } catch (error) { toast(error.message); }
}
$("service-level").addEventListener("change", () => { serviceState.level = $("service-level").value; loadServiceLog(); });
$("service-logger").addEventListener("change", () => { serviceState.logger = $("service-logger").value; loadServiceLog(); });
$("service-clear").addEventListener("click", () => {
  serviceState.level = ""; serviceState.logger = "";
  $("service-level").value = ""; $("service-logger").value = "";
  loadServiceLog();
});
$("service-refresh").addEventListener("click", loadServiceLog);

// ---------------------------------------------------------------------------
// 用量统计
// ---------------------------------------------------------------------------

const SERIES_COLORS = ["#2f6f5e", "#7a9a3f", "#c07b2c", "#8a5a9e", "#3a6ea5"];
const usageState = {range: "7", start: "", end: "", account: "", key: "", metric: "credits", dimension: "models", data: null, loading: false};
const pad2 = value => String(value).padStart(2, "0");
const dayStr = d => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const todayStr = () => dayStr(new Date());
function shiftDays(day, delta) {
  const [y, m, d] = String(day).split("-").map(Number);
  return dayStr(new Date(y, m - 1, d + delta));
}
function metricLabel() { return usageState.metric === "tokens" ? "token" : "积分"; }
function fmtMetric(value) {
  if (value === null || value === undefined) return "—";
  return usageState.metric === "tokens" ? fmtNum(value, 0) : fmtNum(value, 2);
}
function fmtCompact(value) {
  const n = Number(value) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return String(Math.round(n * 100) / 100);
}
function niceMax(value) {
  if (!(value > 0)) return 1;
  const base = Math.pow(10, Math.floor(Math.log10(value)));
  const norm = value / base;
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  return step * base;
}
function chartFrame(days, max) {
  const W = 720, H = 260, L = 58, R = 14, T = 16, B = 32;
  const plotW = W - L - R, plotH = H - T - B;
  const y = value => T + plotH - (max ? (value / max) * plotH : 0);
  const slot = days.length ? plotW / days.length : plotW;
  const center = index => L + slot * index + slot / 2;
  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const value = (max / 4) * i, gy = y(value);
    grid += `<line x1="${L}" y1="${gy.toFixed(1)}" x2="${W - R}" y2="${gy.toFixed(1)}" stroke="#e7ece7" stroke-width="1"/>`;
    grid += `<text x="${L - 8}" y="${gy.toFixed(1)}" text-anchor="end" dominant-baseline="central" font-size="10" fill="#87918d">${fmtCompact(value)}</text>`;
  }
  const step = Math.max(1, Math.ceil(days.length / 8));
  let axis = "";
  days.forEach((day, index) => {
    if (index % step !== 0 && index !== days.length - 1) return;
    axis += `<text x="${center(index).toFixed(1)}" y="${H - B + 14}" text-anchor="middle" font-size="10" fill="#87918d">${esc(day.slice(5))}</text>`;
  });
  return {W, H, L, R, T, B, plotW, plotH, y, slot, center, grid, axis};
}
function renderBarChart(days, values) {
  if (!days.length) return '<p class="history-empty">所选范围内没有数据。</p>';
  const max = niceMax(Math.max(...values, 0));
  const f = chartFrame(days, max);
  const width = Math.max(1, f.slot * 0.62);
  const bars = days.map((day, index) => {
    const value = values[index] || 0;
    const top = f.y(value);
    const height = Math.max(value > 0 ? 1.5 : 0, f.T + f.plotH - top);
    const x = (f.center(index) - width / 2).toFixed(1);
    return `<rect x="${x}" y="${top.toFixed(1)}" width="${width.toFixed(1)}" height="${height.toFixed(1)}" rx="2" fill="#2f6f5e"><title>${esc(day)}　${fmtMetric(value)} ${metricLabel()}</title></rect>`;
  }).join("");
  return `<svg viewBox="0 0 ${f.W} ${f.H}" width="100%" role="img" aria-label="每日用量柱状图">${f.grid}${f.axis}${bars}</svg>`;
}
function renderLineChart(days, series) {
  if (!days.length) return '<p class="history-empty">所选范围内没有数据。</p>';
  const max = niceMax(Math.max(...series.flatMap(item => item.values), 0));
  const f = chartFrame(days, max);
  const lines = series.map((item, index) => {
    const color = SERIES_COLORS[index % SERIES_COLORS.length];
    const points = item.values.map((value, i) => `${f.center(i).toFixed(1)},${f.y(value).toFixed(1)}`).join(" ");
    const dots = item.values.map((value, i) =>
      `<circle cx="${f.center(i).toFixed(1)}" cy="${f.y(value).toFixed(1)}" r="2.5" fill="${color}"><title>${esc(item.model)}　${esc(days[i])}　${fmtMetric(value)} ${metricLabel()}</title></circle>`).join("");
    return `<polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>${dots}`;
  }).join("");
  return `<svg viewBox="0 0 ${f.W} ${f.H}" width="100%" role="img" aria-label="模型用量趋势折线图">${f.grid}${f.axis}${lines}</svg>`;
}
function renderUsageLegend(series) {
  $("usage-legend").innerHTML = series.map((item, index) =>
    `<span><i style="background:${SERIES_COLORS[index % SERIES_COLORS.length]}"></i>${esc(item.model)}</span>`).join("");
}
function renderUsage() {
  const data = usageState.data;
  if (!data) return;
  const totals = data.totals || {};
  $("usage-requests").textContent = fmtNum(totals.requests || 0, 0);
  $("usage-credits").textContent = fmtNum(totals.credits || 0, 2);
  $("usage-tokens").textContent = fmtNum(totals.tokens || 0, 0);
  $("usage-split").textContent = `${fmtCompact(totals.prompt_tokens || 0)} / ${fmtCompact(totals.completion_tokens || 0)}`;
  $("usage-bar-title").textContent = `每日用量（${metricLabel()}）`;
  $("usage-bar-note").textContent = `${data.range.from} ~ ${data.range.to}，共 ${data.range.days} 天`;
  const days = (data.daily || []).map(item => item.date);
  const values = (data.daily || []).map(item => item[usageState.metric] || 0);
  $("usage-bar").innerHTML = renderBarChart(days, values);
  const series = (data.series || []).slice(0, 5).map(item => ({model: item.model, values: item[usageState.metric] || []}));
  $("usage-line").innerHTML = renderLineChart(days, series);
  renderUsageLegend(series);
  renderUsageRank();
  fillUsageFilters(data);
}
function fillUsageFilters(data) {
  const fill = (id, rows, current, allLabel) => {
    const select = $(id);
    const options = [["", allLabel]].concat((rows || []).map(row => [row.name, `${row.name}（${row.requests}）`]));
    select.replaceChildren(...options.map(([value, text]) => { const o = document.createElement("option"); o.value = value; o.textContent = text; return o; }));
    select.value = options.some(([value]) => value === current) ? current : "";
    if (select.value !== current) { usageState[id === "usage-account" ? "account" : "key"] = ""; }
  };
  fill("usage-account", data.accounts, usageState.account, "全部账号");
  fill("usage-key", data.keys, usageState.key, "全部密钥");
}
function renderUsageRank() {
  const data = usageState.data;
  const rows = (data && data[usageState.dimension]) || [];
  const metric = usageState.metric;
  const total = rows.reduce((sum, row) => sum + (row[metric] || 0), 0);
  $("usage-rank-note").textContent = rows.length
    ? `${rows.length} 项，按${metricLabel()}降序，合计 ${fmtMetric(total)} ${metricLabel()}`
    : "所选范围内没有数据";
  $("usage-rank-body").innerHTML = rows.map((row, index) => {
    const value = row[metric] || 0;
    const share = total > 0 ? (value / total) * 100 : 0;
    return `<tr><td class="rank-col mono muted">${index + 1}</td><td>${esc(row.name)}</td><td class="align-right mono">${fmtNum(row.requests, 0)}</td><td class="align-right mono">${fmtNum(row.credits, 2)}</td><td class="align-right mono">${fmtNum(row.tokens, 0)}</td><td><div class="share-cell"><span class="share-bar" style="width:${share.toFixed(1)}%"></span><small class="mono">${share.toFixed(1)}%</small></div></td></tr>`;
  }).join("") || '<tr><td colspan="6" class="history-empty">所选范围内没有数据。</td></tr>';
}
async function loadUsage() {
  if (usageState.loading) return;
  usageState.loading = true;
  $("usage-refresh").disabled = true;
  const params = new URLSearchParams();
  if (usageState.range === "today") {
    params.set("start", todayStr()); params.set("end", todayStr());
  } else if (usageState.range === "custom") {
    if (usageState.start) params.set("start", usageState.start);
    if (usageState.end) params.set("end", usageState.end);
  } else {
    params.set("end", todayStr());
    params.set("start", shiftDays(todayStr(), -(Number(usageState.range) - 1)));
  }
  if (usageState.account) params.set("account", usageState.account);
  if (usageState.key) params.set("key", usageState.key);
  try {
    usageState.data = await api("usage?" + params.toString());
    renderUsage();
  } catch (error) { toast(error.message); }
  finally { usageState.loading = false; $("usage-refresh").disabled = false; }
}
function usageRangeChanged() {
  const custom = usageState.range === "custom";
  $("usage-start").hidden = !custom;
  $("usage-end").hidden = !custom;
  if (custom && !usageState.end) { usageState.end = todayStr(); $("usage-end").value = usageState.end; }
  if (custom && !usageState.start) { usageState.start = shiftDays(todayStr(), -6); $("usage-start").value = usageState.start; }
  loadUsage();
}
$("usage-range").addEventListener("change", () => { usageState.range = $("usage-range").value; usageRangeChanged(); });
$("usage-start").addEventListener("change", () => { usageState.start = $("usage-start").value; loadUsage(); });
$("usage-end").addEventListener("change", () => { usageState.end = $("usage-end").value; loadUsage(); });
$("usage-account").addEventListener("change", () => { usageState.account = $("usage-account").value; loadUsage(); });
$("usage-key").addEventListener("change", () => { usageState.key = $("usage-key").value; loadUsage(); });
$("usage-metric").addEventListener("change", () => { usageState.metric = $("usage-metric").value; renderUsage(); });
$("usage-dimension").addEventListener("change", () => { usageState.dimension = $("usage-dimension").value; renderUsageRank(); });
$("usage-refresh").addEventListener("click", loadUsage);
$("usage-clear").addEventListener("click", () => {
  usageState.account = ""; usageState.key = ""; usageState.range = "7";
  $("usage-range").value = "7"; $("usage-account").value = ""; $("usage-key").value = "";
  usageRangeChanged();
});
$("account-search").addEventListener("input", () => { if(overview) renderAccounts(); });
$("account-filter").addEventListener("change", () => { if(overview) renderAccounts(); });
async function refresh() {
  $("refresh").disabled = true;
  try { overview = await api("overview"); render(); $("load-error").hidden = true; }
  catch (e) { $("load-error").textContent = e.message; $("load-error").hidden = false; throw e; }
  finally { $("refresh").disabled = false; }
}
async function enter() { $("boot").hidden = true; $("login").hidden = true; $("workspace").hidden = false; goPage(location.hash.slice(1)); await refresh(); }
$("login-form").addEventListener("submit", async e => {
  e.preventDefault(); const button = e.submitter; button.disabled = true; $("login-error").textContent = "";
  try { const data = await api("login", {method:"POST",body:{key:$("admin-key").value.trim()}}); csrf = data.csrf; $("admin-key").value = ""; await enter(); }
  catch (error) { $("login-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", async () => { try { await api("logout", {method:"POST"}); showLogin(); } catch (e) { toast(e.message); } });
async function batchAction(action) {
  $("refresh").disabled = $("batch-checkin").disabled = true;
  try {
    const result = await api("pool/actions/"+action,{method:"POST"});
    $("pool-result").hidden = false;
    $("pool-result").textContent = result.results.map(r => `${overview.accounts.find(a=>a.id===r.id)?.name || "账号"}：${r.message}`).join("；") || "没有启用的账号";
    await refresh();
  } catch(e) { toast(e.message); } finally { $("refresh").disabled = $("batch-checkin").disabled = false; }
}
$("refresh").addEventListener("click", () => page === "accounts" ? batchAction("status") : refresh().catch(e=>toast(e.message)));
$("batch-checkin").addEventListener("click", () => batchAction("checkin"));
$("pool-settings").addEventListener("submit", async e => { e.preventDefault();e.submitter.disabled=true;try {await api("pool/settings",{method:"PATCH",body:{routing:$("pool-routing").value,auto_checkin:$("auto-checkin").checked,checkin_time:$("checkin-time").value}});await refresh();toast("账号池设置已保存");}catch(err){toast(err.message);}finally{e.submitter.disabled=false;} });
setInterval(() => { if(csrf && overview && !document.hidden && !document.querySelector("dialog[open]") && !$("refresh").disabled) refresh().catch(()=>{}); },30000);
document.querySelectorAll("[data-page]").forEach(b => b.addEventListener("click", () => goPage(b.dataset.page)));
$("go-guide").addEventListener("click", () => goPage("guide"));
document.querySelectorAll(".close-dialog").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));
function clearOAuth() {
  oauthGeneration++; clearTimeout(oauthTimer);
  const previous = oauthFlow; oauthFlow = null;
  if (previous && csrf) api("oauth/" + previous.id, {method:"DELETE"}).catch(() => {});
  $("oauth-link-box").hidden = true; $("oauth-open").removeAttribute("href");
  $("oauth-start").disabled = false; $("oauth-start").textContent = "生成登录链接 ↗";
  $("oauth-retry").hidden = true; $("account-name").disabled = false;
  $("oauth-status").textContent = "登录链接 5 分钟内有效。同一账号重新登录会更新凭据。";
  $("oauth-status").className = "banner oauth-status";
}
function setAccountMode(mode) {
  clearOAuth(); accountMode = mode;
  $("browser-login-panel").hidden = mode !== "browser";
  $("file-import-panel").hidden = $("account-import").hidden = mode !== "file";
  $("mode-browser").setAttribute("aria-pressed", String(mode === "browser"));
  $("mode-file").setAttribute("aria-pressed", String(mode === "file"));
  $("account-error").textContent = "";
}
$("mode-browser").addEventListener("click", () => setAccountMode("browser"));
$("mode-file").addEventListener("click", () => setAccountMode("file"));
$("account-dialog").addEventListener("close", () => { clearOAuth(); $("account-form").reset(); $("file-label").textContent = "选择或拖入 .info / .json 文件"; $("account-error").textContent = ""; });
function openAccount() { setAccountMode("browser"); $("account-dialog").showModal(); }
async function pollOAuth(generation) {
  if (generation !== oauthGeneration || !oauthFlow) return;
  const flow = oauthFlow;
  if (Date.now() >= flow.expires_at) { clearOAuth(); $("oauth-status").textContent = "登录链接已过期，请重新生成。"; return; }
  try {
    const result = await api("oauth/" + flow.id + "/poll", {method:"POST"});
    if (generation !== oauthGeneration) return;
    if (result.status === "success") {
      $("account-dialog").close(); await refresh();
      toast(result.updated ? "授权成功，账号凭据已更新" : "授权成功，账号已添加；可在列表中切换使用"); return;
    }
    if (result.status === "expired") { clearOAuth(); $("oauth-status").textContent = "登录链接已过期，请重新生成。"; return; }
    $("oauth-status").className = "banner oauth-status";
    $("oauth-status").textContent = `等待你在官方页面完成登录… 链接剩余 ${Math.max(1, Math.ceil((flow.expires_at - Date.now()) / 1000))} 秒。`;
    oauthTimer = setTimeout(() => pollOAuth(generation), 3000);
  } catch (e) {
    if (generation !== oauthGeneration) return;
    $("oauth-status").className = "banner warning oauth-status"; $("oauth-status").textContent = e.message;
    $("oauth-retry").hidden = false;
  }
}
$("oauth-start").addEventListener("click", async () => {
  clearOAuth(); const generation = oauthGeneration;
  $("oauth-start").disabled = true; $("oauth-start").textContent = "正在生成…";
  $("account-error").textContent = "";
  try {
    const flow = await api("oauth/start", {method:"POST",body:{name:$("account-name").value.trim() || undefined}});
    if (generation !== oauthGeneration) { api("oauth/" + flow.id, {method:"DELETE"}).catch(() => {}); return; }
    oauthFlow = flow; $("oauth-open").href = flow.url; $("oauth-link-box").hidden = false;
    $("oauth-start").textContent = "重新生成链接"; $("account-name").disabled = true;
    $("oauth-status").textContent = "登录链接已就绪，请打开官方页面完成登录。";
    oauthTimer = setTimeout(() => pollOAuth(generation), 3000);
  } catch (e) { if (generation === oauthGeneration) $("account-error").textContent = e.message; }
  finally { if (generation === oauthGeneration) $("oauth-start").disabled = false; }
});
$("oauth-copy").addEventListener("click", () => { if (oauthFlow) copy(oauthFlow.url); });
$("oauth-retry").addEventListener("click", () => { $("oauth-retry").hidden = true; pollOAuth(oauthGeneration); });
$("add-account").addEventListener("click", openAccount); $("empty-add").addEventListener("click", openAccount);
async function readFile(file) {
  if (!file) return;
  if (file.size > 1024 * 1024) throw new Error("文件超过 1 MB，请选择登录凭据文件");
  $("credential-json").value = (await file.text()).replace(/^\uFEFF/, "");
  $("file-label").textContent = file.name;
}
$("credential-file").addEventListener("change", e => { readFile(e.target.files[0]).catch(err => $("account-error").textContent = err.message); });
$("file-drop").addEventListener("dragover", e => { e.preventDefault(); $("file-drop").classList.add("drag-over"); });
$("file-drop").addEventListener("dragleave", () => $("file-drop").classList.remove("drag-over"));
$("file-drop").addEventListener("drop", e => { e.preventDefault(); $("file-drop").classList.remove("drag-over"); readFile(e.dataTransfer.files[0]).catch(err => $("account-error").textContent = err.message); });
$("account-form").addEventListener("submit", async e => {
  if (accountMode !== "file") { e.preventDefault(); return; }
  e.preventDefault(); e.submitter.disabled = true; $("account-error").textContent = "";
  try {
    let credential; try { credential = JSON.parse($("credential-json").value); } catch { throw new Error("请选择文件或粘贴有效的 JSON 内容"); }
    await api("accounts", {method:"POST", body:{name:$("account-name").value.trim() || undefined, credential}});
    $("account-dialog").close(); await refresh(); toast("账号已导入");
  } catch (err) { $("account-error").textContent = err.message; } finally { e.submitter.disabled = false; }
});
function confirmAction(title, desc, callback, rename = null) {
  $("confirm-title").textContent = title; $("confirm-desc").textContent = desc; $("confirm-error").textContent = "";
  $("rename-label").hidden = $("rename-value").hidden = rename === null; $("rename-value").value = rename || "";
  pendingConfirm = callback; $("confirm-dialog").showModal();
}
$("confirm-form").addEventListener("submit", async e => { e.preventDefault(); e.submitter.disabled = true; try { await pendingConfirm(); $("confirm-dialog").close(); await refresh(); toast("操作已保存"); } catch (err) { $("confirm-error").textContent = err.message; } finally { e.submitter.disabled = false; } });
$("accounts-body").addEventListener("click", async e => {
  const b = e.target.closest("[data-action]"); if (!b) return;
  const a = overview.accounts.find(x => x.id === b.dataset.id); if (!a) return;
  if (["status","checkin","refresh"].includes(b.dataset.action)) {
    b.disabled = true;
    try { const result=await api(`accounts/${a.id}/actions/${b.dataset.action}`,{method:"POST"});toast(result.message);await refresh(); }
    catch(err){toast(err.message);} finally {b.disabled=false;} return;
  }
  const update = body => api("accounts/" + a.id, {method:"PATCH", body});
  if (b.dataset.action === "activate") confirmAction("设置手动 / 测试账号？", `设为「${a.name}」。仅在手动调度模式和连接测试中使用；账号池轮转模式仍自动分配。`, () => update({active:true}));
  if (b.dataset.action === "rename") confirmAction("编辑账号备注", "备注仅用于在管理后台识别账号。", () => update({name:$("rename-value").value}), a.name);
  if (b.dataset.action === "toggle") confirmAction(a.enabled ? "暂停这个账号？" : "恢复这个账号？", "暂停后不参与新请求分配和自动签到，已开始的请求继续完成。恢复后重新参与轮转。", () => update({enabled:!a.enabled}));
  if (b.dataset.action === "delete") confirmAction("删除这个账号？", `「${a.name}」将从账号列表移除。服务器保留恢复副本。${a.active ? "当前调用账号将被清空。" : ""}`, () => api("accounts/" + a.id, {method:"DELETE"}));
});
$("keys-body").addEventListener("click", e => { const b = e.target.closest("[data-revoke]"); if (!b) return; const k = overview.keys.find(x => x.id === b.dataset.revoke); confirmAction("撤销客户端密钥？", `使用「${k.name}」的客户端将无法发起新请求。此操作不可撤销。`, () => api("keys/" + k.id, {method:"DELETE"})); });
$("add-key").addEventListener("click", () => { $("key-form").hidden = false; $("created-key").hidden = true; $("key-dialog").showModal(); });
$("key-dialog").addEventListener("close", () => { $("key-form").reset(); $("new-key-value").textContent = ""; $("key-error").textContent = ""; });
$("key-form").addEventListener("submit", async e => { e.preventDefault(); e.submitter.disabled = true; try { const d = await api("keys", {method:"POST",body:{name:$("key-name").value}}); $("key-form").hidden = true; $("created-key").hidden = false; $("new-key-value").textContent = d.key; await refresh(); } catch (err) { $("key-error").textContent = err.message; } finally { e.submitter.disabled = false; } });
async function copy(value) { try { await navigator.clipboard.writeText(value); toast("已复制"); } catch { toast("无法自动复制，请手动选择文本复制"); } }
$("copy-key").addEventListener("click", () => copy($("new-key-value").textContent));
document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", () => copy(b.dataset.copy)));
$("base-url").textContent = location.origin + "/v1"; $("anthropic-url").textContent = location.origin;
$("copy-base").addEventListener("click", () => copy(location.origin + "/v1")); $("copy-anthropic").addEventListener("click", () => copy(location.origin));
$("host-label").textContent = location.host;
$("test-form").addEventListener("submit", async e => {
  e.preventDefault(); busy = true; $("test-submit").disabled = true; $("test-submit").textContent = "正在调用…"; $("test-status").className = "pill amber"; $("test-status").textContent = "请求中"; $("test-output").textContent = "正在等待上游响应，最长约 90 秒…"; $("test-meta").textContent = "";
  try { const r = await api("test", {method:"POST",body:{model:$("model").value,prompt:$("test-prompt").value}}); $("test-status").className = "pill " + (r.ok ? "green" : "red"); $("test-status").textContent = r.ok ? "连接成功" : "调用失败"; $("test-output").textContent = r.ok ? r.answer : r.error; $("test-meta").textContent = `${r.seconds}s${r.status ? " · HTTP " + r.status : ""}${r.usage?.total_tokens !== undefined ? " · " + r.usage.total_tokens + " tokens" : ""}`; }
  catch (err) { $("test-status").className = "pill red"; $("test-status").textContent = "请求失败"; $("test-output").textContent = err.message; }
  finally { busy = false; $("test-submit").textContent = "发送测试 ↗"; await refresh().catch(() => {}); }
});
(async () => { try { const s = await api("session"); csrf = s.csrf; await enter(); } catch (e) { if (!csrf) showLogin(); } })();
