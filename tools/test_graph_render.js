/**
 * Render-loop smoke test for the access graph.
 *
 * The DAX harness proves the graph's *query* is valid; it never proves anything
 * is drawn. A blank canvas is exactly what the user saw when a local `alpha`
 * shadowed the global colour helper - every query passed, every test passed, and
 * the pane was empty.
 *
 * This runs PANES.graph against stubbed data with a recording 2D context and
 * asserts that arcs and strokes were actually issued.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DIST = path.join(__dirname, "..", "app", "dist");
const read = (f) => fs.readFileSync(path.join(DIST, f), "utf8");

// ---- recording canvas context -------------------------------------------
const ops = { arc: 0, fill: 0, stroke: 0, fillText: 0, clearRect: 0 };
const ctxStub = new Proxy(
  {
    canvas: {},
    setTransform() {}, save() {}, restore() {}, translate() {}, scale() {},
    beginPath() {}, moveTo() {}, lineTo() {}, measureText: () => ({ width: 10 }),
    arc() { ops.arc++; }, fill() { ops.fill++; }, stroke() { ops.stroke++; },
    fillText() { ops.fillText++; }, clearRect() { ops.clearRect++; },
  },
  { get: (t, k) => (k in t ? t[k] : undefined), set: (t, k, v) => ((t[k] = v), true) }
);

// ---- minimal DOM ---------------------------------------------------------
function makeEl(tag) {
  const style = { setProperty() {}, removeProperty() {}, getPropertyValue: () => "" };
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    children: [], style, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
    clientWidth: 900, clientHeight: 600,
    _html: "",
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    remove() {
      if (!this.parentNode) return;
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) this.parentNode.children.splice(i, 1);
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 600 }),
    getContext: () => ctxStub,
    setAttribute() {}, getAttribute: () => null,
    contains: () => true,
    focus() {}, click() {},
  };
  return node;
}

const body = makeEl("body");
const documentStub = {
  body,
  createElement: makeEl,
  createElementNS: makeEl,
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {},
  documentElement: makeEl("html"),
};

// Stubbed rows: one user reaching two items, one of them through a group, so the
// graph has to build principal, group, workspace and item nodes plus links.
const ROWS = [
  {
    "dim_principal[display_name]": "Ivana", "dim_principal[principal_type]": "User",
    "dim_item[workspace_name]": "WS A", "dim_item[item_name]": "Pipeline_1",
    "dim_item[item_type]": "DataPipeline",
    "fact_effective_access[granted_via_name]": null,
    "fact_effective_access[is_via_group]": false,
    "fact_effective_access[permission_name]": "Admin",
    "fact_effective_access[grant_source]": "WorkspaceRole", "[paths]": 1,
  },
  {
    "dim_principal[display_name]": "Ivana", "dim_principal[principal_type]": "User",
    "dim_item[workspace_name]": "WS B", "dim_item[item_name]": "LH_1",
    "dim_item[item_type]": "Lakehouse",
    "fact_effective_access[granted_via_name]": "Analysts",
    "fact_effective_access[is_via_group]": true,
    "fact_effective_access[permission_name]": "Read",
    "fact_effective_access[grant_source]": "GroupWorkspaceRole", "[paths]": 1,
  },
];

const sandbox = {
  console,
  window: {
    devicePixelRatio: 1, addEventListener() {}, removeEventListener() {},
    location: { origin: "https://x", pathname: "/", hash: "", search: "", href: "https://x/" },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    CONFIG: {
      datasetId: "00000000-0000-0000-0000-000000000000",
      clientId: "00000000-0000-0000-0000-000000000000",
      tenantId: "00000000-0000-0000-0000-000000000000",
      workspaceId: "00000000-0000-0000-0000-000000000000",
    },
  },
  document: documentStub,
  location: { origin: "https://x", pathname: "/", hash: "", search: "", href: "https://x/" },
  navigator: { userAgent: "node" },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  crypto: { getRandomValues: (a) => a, randomUUID: () => "00000000-0000-0000-0000-000000000000" },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  Chart: function () { return { destroy() {} }; },
  msal: { PublicClientApplication: function () { return { initialize: async () => {} }; } },
  setTimeout, clearTimeout, setInterval, clearInterval,
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

// Run the loop a bounded number of times, synchronously, so the test terminates.
let frames = 0;
sandbox.requestAnimationFrame = (fn) => { if (frames++ < 30) fn(); };

const driver = `
globalThis.__run = async function () {
  runDax = async function () { return __ROWS; };
  const root = document.createElement("div");
  document.body.appendChild(root);
  await PANES.graph(root);
  return { nodes: (graphState.nodes || []).length, links: (graphState.links || []).length, root };
};
`;

const ctx = vm.createContext(sandbox);
sandbox.__ROWS = ROWS;

const bundle = [read("onesafe-core.js"), read("onesafe-panes.js"), read("onesafe-panes2.js"), driver].join("\n;\n");

(async () => {
  try {
    vm.runInContext(bundle, ctx, { filename: "bundle.js" });
  } catch (e) {
    console.error("FAIL: bundle did not evaluate:", e.message);
    process.exit(1);
  }

  let res;
  try {
    res = await sandbox.__run();
  } catch (e) {
    console.error("FAIL: PANES.graph threw:", e.message);
    process.exit(1);
  }

  // Collect any error element the pane rendered into itself.
  const errors = [];
  (function walk(n) {
    if (!n || !n.children) return;
    if (n.className === "err" || n.className === "empty") errors.push(n._html || n.textContent || "");
    n.children.forEach(walk);
  })(res.root);

  const problems = [];
  if (errors.length) problems.push("pane reported: " + errors.join(" | "));
  if (!res.nodes) problems.push("no nodes were built");
  if (!res.links) problems.push("no links were built");
  if (!ops.arc) problems.push("no nodes were drawn (ctx.arc never called)");
  if (!ops.stroke) problems.push("no links were drawn (ctx.stroke never called)");
  if (!frames) problems.push("render loop never ran");

  console.log(`nodes=${res.nodes} links=${res.links} frames=${frames} ` +
    `arc=${ops.arc} stroke=${ops.stroke} fill=${ops.fill} label=${ops.fillText}`);

  if (problems.length) {
    console.error("\nFAIL:\n  - " + problems.join("\n  - "));
    process.exit(1);
  }
  console.log("\nGraph renders: nodes and links drawn.");
})();
