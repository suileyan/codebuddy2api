// 用最小 DOM 桩在 node 里跑一遍 app.js 的图表函数，捕获运行时报错。
const fs = require("fs");
const vm = require("vm");

function makeElement() {
  const el = {
    value: "", textContent: "", innerHTML: "", hidden: false, disabled: false,
    scrollTop: 0, scrollHeight: 0, dataset: {},
    classList: {toggle() {}, add() {}, remove() {}},
    style: {},
    addEventListener() {}, removeEventListener() {},
    replaceChildren() {}, append() {}, appendChild() {}, insertAdjacentHTML() {},
    setAttribute() {}, removeAttribute() {}, closest() { return null; },
    contains() { return false; }, querySelectorAll() { return []; }, querySelector() { return makeElement(); },
    focus() {}, click() {}, close() {}, showModal() {},
  };
  return el;
}

const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  },
  querySelectorAll() { return []; },
  querySelector() { return makeElement(); },
  createElement() { return makeElement(); },
  addEventListener() {},
  body: makeElement(),
};

const sandbox = {
  console,
  document,
  window: {matchMedia: () => ({matches: false, addEventListener() {}})},
  location: {hash: ""},
  history: {replaceState() {}},
  fetch: () => Promise.reject(new Error("no network in harness")),
  setTimeout, clearTimeout, setInterval, clearInterval,
  URLSearchParams, JSON, Math, Date, Number, String, Object, Array, Boolean, isNaN, parseInt, parseFloat,
};
sandbox.globalThis = sandbox;

const code = fs.readFileSync("admin/static/app.js", "utf8");
const context = vm.createContext(sandbox);

// app.js 顶层的 const（如 usageState）不会挂到全局对象上，
// 所以测试代码必须拼在同一个脚本里执行，才能共享词法作用域。
const testCode = `
globalThis.__results = [];
const __check = (name, fn) => {
  try {
    const value = fn();
    globalThis.__results.push([name, true, typeof value === "string" ? value.length + " 字符" : JSON.stringify(value)]);
  } catch (error) {
    globalThis.__results.push([name, false, error.message]);
  }
};

const __days = [];
for (let i = 6; i >= 0; i--) __days.push("2026-10-0" + (8 - i));

__check("renderBarChart 正常数据", () => renderBarChart(__days, [0, 1.5, 3, 0, 2.25, 8, 4]));
__check("renderBarChart 全零", () => renderBarChart(__days, [0, 0, 0, 0, 0, 0, 0]));
__check("renderBarChart 空数组", () => renderBarChart([], []));
__check("renderBarChart 单点", () => renderBarChart(["2026-10-08"], [5]));
__check("renderLineChart 多序列", () => renderLineChart(__days, [
  {model: "hy3", values: [1, 2, 3, 4, 5, 6, 7]},
  {model: "glm-5.3", values: [0, 0, 1, 1, 0, 2, 3]},
]));
__check("renderLineChart 空序列", () => renderLineChart(__days, []));
__check("renderLineChart 90 天", () => {
  const many = [], values = [];
  for (let i = 0; i < 90; i++) { many.push("2026-07-" + String((i % 28) + 1).padStart(2, "0")); values.push(i * 3); }
  return renderLineChart(many, [{model: "x", values}]);
});
__check("fmtCompact", () => [0, 999, 1500, 2500000, 3400000000].map(fmtCompact).join(","));
__check("niceMax", () => [0, 1, 7, 23, 150, 980].map(niceMax).join(","));
__check("shiftDays 跨月", () => shiftDays("2026-10-08", -13));
__check("shiftDays 跨年", () => shiftDays("2026-01-03", -7));

__check("renderUsage 整页渲染", () => {
  usageState.data = {
    range: {from: "2026-10-02", to: "2026-10-08", days: 7},
    daily: __days.map((d, i) => ({date: d, requests: i, credits: i * 0.5, tokens: i * 1000})),
    models: [{name: "hy3", requests: 10, credits: 0, tokens: 5000},
             {name: "glm-5.3", requests: 5, credits: 2.5, tokens: 900}],
    accounts: [{name: "a1", requests: 8, credits: 1, tokens: 3000}],
    keys: [{name: "k1", requests: 8, credits: 1, tokens: 3000}],
    series: [{model: "hy3", credits: [0,0,0,0,0,0,0], tokens: [1,2,3,4,5,6,7]},
             {model: "glm-5.3", credits: [1,2,3,4,5,6,7], tokens: [7,6,5,4,3,2,1]}],
    totals: {requests: 15, credits: 2.5, tokens: 5900, prompt_tokens: 4000, completion_tokens: 1900},
  };
  renderUsage();
  return "ok";
});
__check("切换 token 口径重绘", () => { usageState.metric = "tokens"; renderUsage(); usageState.metric = "credits"; return "ok"; });
__check("切换排行维度", () => {
  usageState.dimension = "accounts"; renderUsageRank();
  usageState.dimension = "keys"; renderUsageRank();
  usageState.dimension = "models"; renderUsageRank();
  return "ok";
});
__check("renderUsageRank 空数据", () => {
  usageState.data = {range: {from: "x", to: "y", days: 1}, daily: [], models: [], accounts: [], keys: [], series: [], totals: {}};
  renderUsageRank();
  return "ok";
});
__check("renderUsage 未加载数据时不崩", () => { usageState.data = null; renderUsage(); return "ok"; });
`;

vm.runInContext(code + "\n" + testCode, context, {filename: "app.js"});

const results = sandbox.__results || [];
let failed = 0;
for (const [name, ok, detail] of results) {
  if (!ok) failed++;
  console.log(`  ${ok ? "OK  " : "FAIL"}  ${name}  —  ${detail}`);
}
console.log(failed ? `\n  ${failed} 项失败` : "\n  全部通过");
process.exit(failed ? 1 : 0);
