/* Reproduce the browser's first 30ms against the *deployed* assets.
 *
 * The reported failure ("_msal is not defined") was a bundle that never loaded,
 * which is invisible to any test that reads local source. This fetches what the
 * host actually serves, constructs MSAL exactly as the page does, and prints the
 * redirect URI it derives - the value Entra must match character for character.
 *
 * Usage: node tools/verify_signin.js [hosting-url]
 *
 * The hosting URL defaults to the one recorded by the last `rayfin up` in
 * app/rayfin/.deployments.json, so this works in any tenant without editing.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const https = require("https");

function resolveBase() {
  if (process.argv[2]) return process.argv[2].replace(/\/+$/, "");

  const deployments = path.join(__dirname, "..", "app", "rayfin", ".deployments.json");
  try {
    const raw = JSON.parse(fs.readFileSync(deployments, "utf8"));
    const byName = raw.deployments || raw;

    // Prefer the active deployment: earlier entries can be partial records from
    // a failed first attempt and carry no hostingUrl at all.
    const ordered = [
      byName[raw.active],
      ...Object.values(byName),
    ].filter(Boolean);

    for (const entry of ordered) {
      if (entry.hostingUrl) return String(entry.hostingUrl).replace(/\/+$/, "");
    }
  } catch {
    /* fall through to the message below */
  }

  console.error(
    "No hosting URL found.\n" +
      "  Pass it explicitly:  node tools/verify_signin.js https://<app>.webapp.fabricapps.net\n" +
      "  or deploy first:     cd app && npx rayfin up"
  );
  process.exit(2);
}

const BASE = resolveBase();

function get(url) {
  return new Promise((resolve, reject) => {
    https
      .get(url, (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          return reject(new Error(`${url} -> HTTP ${res.statusCode}`));
        }
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (c) => (body += c));
        res.on("end", () => resolve(body));
      })
      .on("error", reject);
  });
}

// MSAL touches a fair amount of browser surface during construction. Rather than
// guess at the list, hand it a permissive stub and let anything unexpected
// surface as a real error instead of a silent undefined.
function browserEnv(origin, pathname) {
  const store = new Map();
  const storage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
    key: (i) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  };

  const node = () => {
    const n = {
      style: {},
      dataset: {},
      children: [],
      classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
      setAttribute() {},
      getAttribute: () => null,
      appendChild(c) {
        n.children.push(c);
        return c;
      },
      removeChild() {},
      addEventListener() {},
      removeEventListener() {},
      querySelector: () => null,
      querySelectorAll: () => [],
      remove() {},
      focus() {},
      click() {},
      innerHTML: "",
      textContent: "",
    };
    return n;
  };

  const win = {
    location: {
      origin,
      pathname,
      href: origin + pathname,
      hash: "",
      search: "",
      protocol: "https:",
      host: new URL(origin).host,
      hostname: new URL(origin).hostname,
      assign() {},
      replace() {},
    },
    navigator: {
      userAgent:
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
      language: "en-US",
    },
    sessionStorage: storage,
    localStorage: storage,
    crypto: require("crypto").webcrypto,
    performance: { now: () => Date.now() },
    addEventListener() {},
    removeEventListener() {},
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    fetch: () => Promise.reject(new Error("network disabled in simulation")),
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    open: () => null,
    history: { replaceState() {}, pushState() {} },
  };

  const doc = Object.assign(node(), {
    getElementById: () => node(),
    createElement: () => node(),
    createTextNode: () => node(),
    head: node(),
    body: node(),
    documentElement: node(),
    readyState: "complete",
  });

  win.window = win;
  win.self = win;
  win.top = win;
  win.parent = win;
  win.document = doc;
  win.globalThis = win;
  return win;
}

(async () => {
  const assets = [
    "vendor/msal-browser.min.js",
    "vendor/chart.umd.min.js",
    "config.js",
    "onesafe-core.js",
    "index.html",
  ];

  const fetched = {};
  for (const a of assets) {
    fetched[a] = await get(`${BASE}/${a}`);
    console.log(`  fetched ${a.padEnd(30)} ${fetched[a].length} bytes`);
  }

  // The page loads MSAL from vendor/, not a CDN. A regression here is exactly
  // what produced the original failure, so assert it rather than assume it.
  const html = fetched["index.html"];
  const cdn = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map((m) => m[1]);
  const external = cdn.filter((s) => /^https?:/.test(s));
  console.log(`\n  script sources: ${cdn.join(", ")}`);
  if (external.length) {
    console.log(`  FAIL: still loading from external hosts: ${external.join(", ")}`);
    process.exit(1);
  }

  const origin = BASE;
  for (const pathname of ["/", "/index.html"]) {
    const env = browserEnv(origin, pathname);
    vm.createContext(env);

    vm.runInContext(fetched["vendor/msal-browser.min.js"], env, { filename: "msal-browser.min.js" });
    if (typeof env.msal !== "object" || typeof env.msal.PublicClientApplication !== "function") {
      console.log(`  FAIL: msal global missing after load (${typeof env.msal})`);
      process.exit(1);
    }
    vm.runInContext(fetched["vendor/chart.umd.min.js"], env, { filename: "chart.umd.min.js" });
    if (typeof env.Chart !== "function") {
      console.log("  FAIL: Chart global missing after load");
      process.exit(1);
    }

    vm.runInContext(fetched["config.js"], env, { filename: "config.js" });

    // onesafe-core declares _msal with const at top level, so it is lexically
    // scoped to the script and never lands on the sandbox global. Append a probe
    // inside the same script to read it out.
    const probe = `
      ;globalThis.__probe = {
        redirectUri: _msal.getConfiguration().auth.redirectUri,
        clientId: _msal.getConfiguration().auth.clientId,
        authority: _msal.getConfiguration().auth.authority,
        scopes: CFG.pbiScopes,
        datasetId: CFG.datasetId,
      };`;
    vm.runInContext(fetched["onesafe-core.js"] + probe, env, { filename: "onesafe-core.js" });

    const p = env.__probe;
    console.log(`\n  page ${pathname}`);
    console.log(`    _msal constructed : yes`);
    console.log(`    redirectUri       : ${p.redirectUri}`);
    console.log(`    clientId          : ${p.clientId}`);
    console.log(`    authority         : ${p.authority}`);
    console.log(`    scopes            : ${JSON.stringify(p.scopes)}`);
    console.log(`    datasetId         : ${p.datasetId}`);

    fs.writeFileSync(
      path.join(__dirname, "_signin_probe.json"),
      JSON.stringify(p, null, 2)
    );
  }

  console.log("\nOK: deployed bundle loads and MSAL initialises.");
})().catch((e) => {
  console.error("FAILED: " + (e && e.stack ? e.stack : e));
  process.exit(1);
});
