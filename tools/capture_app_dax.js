/* Headless harness that runs every OneSafe pane against a fake DOM and captures
 * the DAX it issues.
 *
 * Why this exists: the panes are the only part of OneSafe whose queries are
 * composed at runtime from filter state, so a query can be wrong in a way no
 * amount of reading catches - a column renamed in the model, a measure that
 * doesn't exist, a filter that doesn't propagate. Signing in and clicking eight
 * tabs by hand tests one filter combination and doesn't scale.
 *
 * Instead we stub the browser, run each pane under several filter states, and
 * dump every generated query to JSON. tools/check_app_dax.py then executes them
 * against the live model, so a broken query surfaces as a failing check rather
 * than as an empty panel an admin has to notice.
 *
 * Usage: node tools/capture_app_dax.js > tools/_app_queries.json
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DIST = path.join(__dirname, "..", "app", "dist");
const queries = [];
let currentLabel = "unknown";

/* ---------------------------------------------------------------- fake DOM */

// The panes only ever build elements, set text/HTML, attach handlers and append
// children. A permissive Proxy models that exactly without pulling in jsdom.
function makeElement(tag = "div") {
  const node = {
    tagName: String(tag).toUpperCase(),
    style: new Proxy({}, { get: () => "", set: () => true }),
    dataset: {},
    children: [],
    className: "",
    innerHTML: "",
    textContent: "",
    value: "",
    checked: false,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild(c) { this.children.push(c); return c; },
    append(...c) { this.children.push(...c); },
    removeChild() {},
    remove() {},
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    getAttribute: () => null,
    querySelector: () => makeElement(),
    querySelectorAll: () => [],
    closest: () => null,
    focus() {},
    click() {},
    scrollIntoView() {},
    getBoundingClientRect: () => ({ width: 900, height: 500, top: 0, left: 0 }),
    // Canvas panes ask for a 2d context; hand back a no-op sink.
    getContext: () => new Proxy({}, {
      get: () => () => ({}),
    }),
  };
  return new Proxy(node, {
    get(t, k) {
      if (k in t) return t[k];
      if (typeof k === "symbol") return undefined;
      return () => undefined;
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}

const documentStub = {
  createElement: (t) => makeElement(t),
  createElementNS: (_ns, t) => makeElement(t),
  createDocumentFragment: () => makeElement("fragment"),
  getElementById: () => makeElement(),
  querySelector: () => makeElement(),
  querySelectorAll: () => [],
  addEventListener() {},
  body: makeElement("body"),
  documentElement: makeElement("html"),
};

/* ------------------------------------------------------------- module load */

const sandbox = {
  console,
  document: documentStub,
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval: () => {},
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {},
  devicePixelRatio: 1,
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  location: {
    href: "https://example.invalid/",
    origin: "https://example.invalid",
    pathname: "/",
    hash: "",
    search: "",
  },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  // The app reads its palette from CSS custom properties. There is no
  // stylesheet here, so return empty and let the coded fallbacks apply.
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  msal: {
    PublicClientApplication: class {
      async initialize() {}
      async handleRedirectPromise() { return null; }
      getActiveAccount() { return null; }
      getAllAccounts() { return []; }
      setActiveAccount() {}
      loginRedirect() {}
      logoutRedirect() {}
      async acquireTokenSilent() { return { accessToken: "stub", expiresOn: new Date(Date.now() + 3e6) }; }
      async acquireTokenRedirect() {}
    },
  },
  Chart: class { constructor() {} destroy() {} update() {} },
  __capture: (pane, dax) => queries.push({ pane, dax }),
  __makeElement: makeElement,
  __report: null,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

// The app files use top-level `const`/`let`, which in a VM script are lexically
// scoped and never land on the sandbox global. Concatenating them with the
// driver into a single script puts everything in one scope, which also lets the
// driver reassign `runDax` - the whole point of the harness.
const sources = ["config.js", "onesafe-core.js", "onesafe-panes.js", "onesafe-panes2.js"]
  .map((f) => `/* ==== ${f} ==== */\n` + fs.readFileSync(path.join(DIST, f), "utf8"))
  .join("\n");

// index.html carries two queries of its own - the snapshot list and the data
// freshness indicator - which no pane issues. Left out, the two queries every
// admin hits before seeing anything else would be the only untested ones. Pull
// the inline bootstrap out of the page and include it, minus its startup IIFE
// (which would drive sign-in rather than compose DAX).
const html = fs.readFileSync(path.join(DIST, "index.html"), "utf8");
const inline = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
  .map((m) => m[1])
  .filter((s) => s.includes("loadSnapshots"))
  .map((s) => s.replace(/\(async function \(\)[\s\S]*$/, ""))
  .join("\n");

if (!inline.includes("loadFreshness")) {
  console.error("warning: could not extract index.html bootstrap - its queries are untested");
}

/* --------------------------------------------------------------- run panes */

// Each state exercises a different composition path: unfiltered, a principal
// focus, an item/workspace focus, and a stacked multi-filter case.
const driver = `
/* ==== harness driver ==== */
let __label = "unknown";

// Capture the query, then return an empty-but-well-formed result so the pane
// keeps rendering and we reach the queries that come after it.
runDax = async function (dax) { __capture(__label, dax); return []; };
// $ is a const arrow over document.getElementById, which the stub already
// satisfies, so it needs no override.

const __STATES = [
  { label: "unfiltered", apply: () => {} },
  { label: "principal-focus", apply: (s) => {
      s.principalId = "00000000-0000-0000-0000-000000000001";
      s.principal = "Test Principal";
      s.ptype = "User";
    } },
  { label: "item-focus", apply: (s) => {
      s.itemId = "00000000-0000-0000-0000-000000000002";
      s.item = "Test Item";
      s.itemType = "SemanticModel";
      s.workspace = "Test Workspace";
    } },
  { label: "stacked", apply: (s) => {
      s.permission = "Read";
      s.source = "WorkspaceRole";
      s.risk = "GuestAccess";
      s.viaGroup = "Yes";
      s.restricted = "Yes";
      s.capacity = "Test Capacity";
    } },
];

// Compare only queries once both slots are filled, so seed it directly rather
// than leaving the one pane that needs two selections untested.
compareState.a = { id: "00000000-0000-0000-0000-000000000001", name: "A" };
compareState.b = { id: "00000000-0000-0000-0000-000000000002", name: "B" };

__report = (async () => {
  const names = Object.keys(PANES);
  if (!names.length) throw new Error("no panes found - did the module names change?");

  // The bootstrap runs before any pane, so test it first.
  __label = "bootstrap/unfiltered";
  for (const fn of [loadSnapshots, loadFreshness]) {
    try { await fn(); } catch (err) {
      console.error("[bootstrap error] " + (err && err.stack || err));
    }
  }

  for (const st of __STATES) {
    for (const name of names) {
      for (const k of Object.keys(state.filters)) delete state.filters[k];
      st.apply(state.filters);
      __label = name + "/" + st.label;
      try {
        await PANES[name](__makeElement());
      } catch (err) {
        __capture(__label, null);
        console.error("[pane error] " + __label + ": " + (err && err.stack || err));
      }
    }
  }
  return names.length;
})();
`;

vm.runInContext(sources + inline + driver, sandbox, { filename: "onesafe-bundle.js" });

sandbox.__report
  .then((n) => {
    const kept = queries.filter((q) => q.dax);
    const out = path.join(__dirname, "_app_queries.json");
    fs.writeFileSync(out, JSON.stringify(kept, null, 1), "utf8");
    console.error(`ran ${n} panes x 4 filter states -> ${kept.length} queries -> ${out}`);
  })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
