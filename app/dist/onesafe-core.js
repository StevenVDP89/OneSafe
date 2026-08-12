/* OneSafe core: auth, DAX execution, global filter state, render helpers.
 *
 * Every visual in this app is hand-built from live DAX issued through the
 * Power BI executeQueries API under the signed-in user's identity, so the app
 * can never show more than the user is entitled to see in the model itself.
 */

const CFG = window.CONFIG;
const EXEC_URL = `https://api.powerbi.com/v1.0/myorg/datasets/${CFG.datasetId}/executeQueries`;

const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};

/* ------------------------------------------------------------------ auth */

/* Deriving the redirect URI from the live pathname keeps localhost and the
 * hosted origin working without separate builds, but Entra matches redirect
 * URIs exactly - so every reachable spelling of the same page would need its
 * own registration. Collapse "/index.html" to "/" and the set stays at one. */
const _redirectUri =
  window.location.origin + window.location.pathname.replace(/index\.html$/i, "");

const _msal = new msal.PublicClientApplication({
  auth: {
    clientId: CFG.clientId,
    authority: `https://login.microsoftonline.com/${CFG.tenantId}`,
    redirectUri: _redirectUri,
  },
  cache: { cacheLocation: "sessionStorage" },
});

let account = null;
let _tok = null;
let _tokExp = 0;

async function getToken() {
  if (_tok && Date.now() < _tokExp - 60000) return _tok;
  try {
    const r = await _msal.acquireTokenSilent({ scopes: CFG.pbiScopes, account });
    _tok = r.accessToken;
    _tokExp = r.expiresOn ? new Date(r.expiresOn).getTime() : Date.now() + 3e6;
    return _tok;
  } catch (e) {
    await _msal.acquireTokenRedirect({ scopes: CFG.pbiScopes, account });
    return null;
  }
}

/* ------------------------------------------------------------------ DAX */

// Identical queries fire from several panes on the same render pass; caching
// per filter-state keeps the executeQueries call count (and latency) sane.
const _daxCache = new Map();
let _cacheEpoch = 0;

async function runDax(dax, { cache = true } = {}) {
  const key = `${_cacheEpoch}|${dax}`;
  if (cache && _daxCache.has(key)) return _daxCache.get(key);

  const p = (async () => {
    const token = await getToken();
    const res = await fetch(EXEC_URL, {
      method: "POST",
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify({
        queries: [{ query: dax }],
        serializerSettings: { includeNulls: true },
      }),
    });
    if (!res.ok) {
      const t = await res.text();
      let msg = t;
      try {
        const j = JSON.parse(t);
        msg = j.error?.["pbi.error"]?.details?.[0]?.detail?.value || j.error?.message || t;
      } catch (_) {}
      throw new Error(msg.slice(0, 400));
    }
    const json = await res.json();
    return json.results?.[0]?.tables?.[0]?.rows || [];
  })();

  if (cache) _daxCache.set(key, p);
  try {
    return await p;
  } catch (e) {
    _daxCache.delete(key);
    throw e;
  }
}

// Rows come back keyed as "table[column]" or "[measure]"; the callers only
// care about the trailing name, so resolve by suffix.
const pick = (row, suffix) => {
  if (row == null) return null;
  const k = Object.keys(row).find((k) => k.endsWith(suffix));
  return k ? row[k] : null;
};
const val = (rows, suffix, dflt = 0) => {
  const v = pick(rows?.[0], suffix);
  return v === null || v === undefined ? dflt : v;
};

/* ------------------------------------------------------------ DAX escaping */

const q = (s) => String(s ?? "").replace(/"/g, '""');

/* ------------------------------------------------------- global filters */

/* Cross-filtering is done server-side: every pane composes its DAX with the
 * shared filter predicates, so a click in one visual narrows every other one.
 */
const FILTER_DEFS = {
  principal:  { label: "Principal",  expr: (v) => `'dim_principal'[display_name] = "${q(v)}"`, owns: ["principalId"] },
  principalId:{ label: "Principal",  expr: (v) => `'dim_principal'[principal_id] = "${q(v)}"`, hiddenBy: "principal" },
  ptype:      { label: "Type",       expr: (v) => `'dim_principal'[principal_type] = "${q(v)}"` },
  workspace:  { label: "Workspace",  expr: (v) => `'dim_workspace'[workspace_name] = "${q(v)}"` },
  itemType:   { label: "Item type",  expr: (v) => `'dim_item'[item_type] = "${q(v)}"` },
  item:       { label: "Item",       expr: (v) => `'dim_item'[item_name] = "${q(v)}"`, owns: ["itemId"] },
  itemId:     { label: "Item",       expr: (v) => `'dim_item'[item_id] = "${q(v)}"`, hiddenBy: "item" },
  capacity:   { label: "Capacity",   expr: (v) => `'dim_capacity'[capacity_name] = "${q(v)}"` },
  permission: { label: "Permission", expr: (v) => `'fact_effective_access'[permission_name] = "${q(v)}"` },
  source:     { label: "Grant via",  expr: (v) => `'fact_effective_access'[grant_source] = "${q(v)}"` },
  risk:       { label: "Risk",       expr: (v) => `SEARCH("${q(v)}", 'fact_effective_access'[risk_flags], 1, 0) > 0` },
  viaGroup:   { label: "Via group",  expr: () => `'fact_effective_access'[is_via_group] = TRUE()` },
  restricted: { label: "OneLake restricted", expr: () => `'fact_effective_access'[data_plane_restricted] = TRUE()` },
};

const state = {
  filters: {},          // key -> value
  snapshot: null,       // selected snapshot_date, null = latest
  snapshots: [],
};

function setFilter(key, value) {
  if (value === null || value === undefined || state.filters[key] === value) {
    delete state.filters[key];
    // A name filter carries an ID filter alongside it (names are not unique, so
    // the ID is what the panes actually resolve against). Clearing the chip the
    // user can see must clear the one they cannot, or the pane stays filtered
    // with nothing on screen to explain why.
    (FILTER_DEFS[key]?.owns || []).forEach((k) => delete state.filters[k]);
  } else {
    state.filters[key] = value;
    // Setting a name by itself invalidates any ID left over from a previous
    // selection - otherwise the two disagree and the result is silently empty.
    (FILTER_DEFS[key]?.owns || []).forEach((k) => delete state.filters[k]);
  }
  _cacheEpoch++;
  renderFilterBar();
  refreshActivePane();
}

function clearFilters() {
  state.filters = {};
  _cacheEpoch++;
  renderFilterBar();
  refreshActivePane();
}

/** Filter predicates, excluding the given keys (so a visual can stay
 *  un-self-filtered and keep showing its full domain). */
function filterExprs(exclude = []) {
  return Object.entries(state.filters)
    .filter(([k]) => !exclude.includes(k))
    .map(([k, v]) => FILTER_DEFS[k].expr(v));
}

function snapshotExpr() {
  return state.snapshot
    ? `'dim_date'[snapshot_date] = "${q(state.snapshot)}"`
    : `'dim_date'[is_latest] = TRUE()`;
}

/** Wrap a DAX expression in CALCULATE with the current filter context. */
function withFilters(expr, exclude = []) {
  const parts = [snapshotExpr(), ...filterExprs(exclude)];
  return `CALCULATE(${expr}${parts.map((p) => ", " + p).join("")})`;
}

/** Build a CALCULATETABLE(...) around a table expression. */
function withFiltersTable(tableExpr, exclude = []) {
  const parts = [snapshotExpr(), ...filterExprs(exclude)];
  return `CALCULATETABLE(${tableExpr}${parts.map((p) => ", " + p).join("")})`;
}

function renderFilterBar() {
  const bar = $("filterBar");
  bar.innerHTML = "";
  const keys = Object.keys(state.filters);

  const snapWrap = el("div", "", "");
  snapWrap.style.cssText = "display:flex;align-items:center;gap:8px;margin-right:6px";
  snapWrap.appendChild(el("span", "fb-label", "Snapshot"));
  const sel = el("select");
  sel.className = "btn-sm";
  sel.style.cssText = "padding:5px 9px;font-size:12px";
  const latest = el("option", "", "Latest");
  latest.value = "";
  sel.appendChild(latest);
  state.snapshots.forEach((s) => {
    const o = el("option", "", s);
    o.value = s;
    if (state.snapshot === s) o.selected = true;
    sel.appendChild(o);
  });
  sel.onchange = () => {
    state.snapshot = sel.value || null;
    _cacheEpoch++;
    refreshActivePane();
  };
  snapWrap.appendChild(sel);
  bar.appendChild(snapWrap);

  if (!keys.length) {
    bar.appendChild(el("span", "fb-label", "No filters — click any visual to cross-filter"));
    return;
  }
  // An ID filter is an implementation detail of its name filter: showing both
  // gives two chips labelled "Principal" for what the user made as one choice.
  // Only surface the ID chip if its owner is absent, so a filter can never
  // become invisible and therefore unremovable.
  const visible = keys.filter((k) => {
    const d = FILTER_DEFS[k];
    return !(d && d.hiddenBy && state.filters[d.hiddenBy] !== undefined);
  });
  bar.appendChild(el("span", "fb-label", "Filters"));
  visible.forEach((k) => {
    const d = FILTER_DEFS[k];
    const chip = el("span", "chip");
    const isFlag = d.expr.length === 0;
    chip.innerHTML = `<span class="dim">${d.label}</span>${isFlag ? "" : escapeHtml(state.filters[k])}`;
    const x = el("span", "x", "&times;");
    x.onclick = () => setFilter(k, null);
    chip.appendChild(x);
    bar.appendChild(chip);
  });
  const clear = el("button", "ghost", "Clear all");
  clear.onclick = clearFilters;
  bar.appendChild(clear);
}

/* --------------------------------------------------------------- format */

const nf = (n) => (Number(n) || 0).toLocaleString();
const compactNum = (n) => {
  n = Number(n) || 0;
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (a >= 1e4) return (n / 1e3).toFixed(1) + "K";
  return n.toLocaleString();
};
const pctFmt = (n, d = 1) => ((Number(n) || 0) * 100).toFixed(d) + "%";

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

const PERM_CLASS = {
  Admin: "b-admin", Owner: "b-admin", Reshare: "b-reshare",
  Write: "b-write", Build: "b-build", Explore: "b-build", Read: "b-read",
};
const permBadge = (p) =>
  `<span class="badge ${PERM_CLASS[p] || "b-neutral"}">${escapeHtml(p || "—")}</span>`;

/* Strength order, mirroring the permission_scale in the gold model. Used to sort
 * collapsed permission lists so the strongest right is read first - an admin
 * scanning a column cares about the ceiling, not the alphabet. */
const PERM_RANK = {
  None: 0, Read: 1, Build: 2, Explore: 2, Write: 3, Reshare: 4, Admin: 5, Owner: 5,
};

/** Render a ";"-joined permission list, strongest first. */
const permBadges = (v) => {
  const parts = splitList(v);
  if (!parts.length) return permBadge(null);
  parts.sort((a, b) => (PERM_RANK[b] ?? 0) - (PERM_RANK[a] ?? 0) || a.localeCompare(b));
  return parts.map(permBadge).join(" ");
};

/**
 * How an item's OneLake Security scan turned out.
 *
 * "0 roles" and "this item cannot hold roles" look identical in a role count but
 * mean opposite things: the first is an item deliberately left open, the second
 * is an item where data-plane scoping is not even switched on. An admin needs to
 * tell them apart, so the status is shown rather than inferred.
 */
const COVERAGE_BADGE = {
  Ok: ['b-ok', 'read'],
  FeatureDisabled: ['b-warn', 'OneLake security off'],
  NotSupported: ['b-neutral', 'not applicable'],
  AccessDenied: ['b-risk', 'denied'],
  Error: ['b-risk', 'error'],
};

const coverageBadge = (v) => {
  const [cls, label] = COVERAGE_BADGE[String(v ?? 'Ok')] || COVERAGE_BADGE.Error;
  return `<span class="badge ${cls}">${escapeHtml(label)}</span>`;
};

/** Split a ";"-joined aggregate into distinct, non-empty, trimmed values. */
function splitList(v) {
  if (v === null || v === undefined) return [];
  return [...new Set(String(v).split(";").map((s) => s.trim()).filter(Boolean))];
}

const RISK_LABEL = {
  GuestAccess: "Guest",
  OrphanedPrincipal: "Orphaned",
  ServicePrincipalWriteAccess: "SPN write",
  ItemResharePrivilege: "Item reshare",
  BroadGroupGrant: "Broad group",
  GroupGrantOnSecuredData: "Group on secured data",
};
function riskBadges(flags) {
  if (!flags) return '<span class="badge b-ok">clean</span>';
  return String(flags)
    .split(";")
    .filter(Boolean)
    .map((f) => `<span class="badge b-risk">${escapeHtml(RISK_LABEL[f] || f)}</span>`)
    .join(" ");
}

/** Render "A -> B -> C" access paths with highlighted arrows. */
function renderPath(p) {
  if (!p) return '<span class="dim">—</span>';
  return (
    '<span class="path">' +
    String(p)
      .split("->")
      .map((s) => `<span class="seg">${escapeHtml(s.trim())}</span>`)
      .join('<span class="arrow">&rsaquo;</span>') +
    "</span>"
  );
}

/** Render several access paths, one per line. */
function renderPaths(v) {
  const parts = splitList(v);
  if (!parts.length) return '<span class="dim">—</span>';
  return parts.map((p) => `<div class="path-line">${renderPath(p)}</div>`).join("");
}

/** Render a ";"-joined list of grant routes. */
const viaBadges = (v) => {
  const parts = splitList(v);
  if (!parts.length) return '<span class="badge b-neutral">direct</span>';
  return parts
    .map((p) =>
      p === "direct"
        ? '<span class="badge b-neutral">direct</span>'
        : `<span class="badge b-group">${escapeHtml(p)}</span>`
    )
    .join(" ");
};

/* ------------------------------------------------------------- collapsing */

/* The fact table's grain is one row per *route* by which a principal reaches an
 * item, which is what lets OneSafe answer "why does this person have access?".
 * Shown raw, though, a single item repeats once per route - Ivana appearing on
 * Pipeline_1 twice, as Admin and as Reshare, reads as duplication rather than as
 * two genuine grants. Collapse to one row per entity and fold the differing
 * columns into lists, so the routes are still all there but the row count
 * matches what an admin would count by eye.
 */
function collapseRows(rows, keyCols, spec = {}) {
  const merge = spec.merge || [];   // union into ";"-joined distinct lists
  const sum = spec.sum || [];       // numeric totals
  const any = spec.any || [];       // true if true for any route
  const first = spec.first || [];   // identical across the group; keep as-is
  // pair: {key, cols} - keeps per-route values together as tuples instead of
  // unioning each column separately. Merging "Admin;Reshare" in one column and
  // two routes in another loses which permission came from which route, which
  // is precisely the question an admin is asking.
  const pair = spec.pair || null;

  const out = new Map();
  for (const r of rows) {
    const key = keyCols.map((k) => String(pick(r, k) ?? "")).join("\u0000");
    let g = out.get(key);
    if (!g) {
      g = { __sets: {}, __routes: 0 };
      keyCols.forEach((k) => (g[k] = pick(r, k)));
      first.forEach((k) => (g[k] = pick(r, k)));
      merge.forEach((k) => (g.__sets[k] = new Set()));
      sum.forEach((k) => (g[k] = 0));
      any.forEach((k) => (g[k] = false));
      if (pair) { g[pair.key] = []; g.__pairSeen = new Set(); }
      out.set(key, g);
    }
    g.__routes++;
    for (const k of merge) {
      // Values may themselves already be lists (risk_flags is ";"-joined).
      splitList(pick(r, k)).forEach((v) => g.__sets[k].add(v));
    }
    for (const k of sum) g[k] += Number(pick(r, k)) || 0;
    for (const k of any) g[k] = g[k] || !!pick(r, k);
    if (pair) {
      const tuple = {};
      pair.cols.forEach((k) => (tuple[k] = pick(r, k)));
      // Identical routes can legitimately repeat across snapshot rows; dedupe
      // so the display does not re-introduce the duplication we just removed.
      const sig = pair.cols.map((k) => String(tuple[k] ?? "")).join("\u0000");
      if (!g.__pairSeen.has(sig)) { g.__pairSeen.add(sig); g[pair.key].push(tuple); }
    }
  }

  return [...out.values()].map((g) => {
    for (const k of Object.keys(g.__sets)) g[k] = [...g.__sets[k]].join(";");
    delete g.__sets;
    delete g.__pairSeen;
    g["[routes]"] = g.__routes;
    delete g.__routes;
    return g;
  });
}

/**
 * Render grant routes as permission-and-path pairs.
 *
 * Each line reads "<permission> via <how> — <path>", so the permission is
 * physically attached to the route that produced it. Previously these were
 * three independent columns and the reader had to guess the correspondence.
 */
function renderGrantRoutes(routes) {
  if (!Array.isArray(routes) || !routes.length) return '<span class="dim">—</span>';
  const ordered = [...routes].sort(
    (a, b) =>
      (PERM_RANK[b["[permission_name]"]] ?? 0) - (PERM_RANK[a["[permission_name]"]] ?? 0)
  );
  return (
    '<div class="routes">' +
    ordered
      .map((t) => {
        const perm = permBadge(t["[permission_name]"]);
        const via = t["[granted_via_name]"];
        const viaHtml = via
          ? `<span class="badge b-group">via ${escapeHtml(via)}</span>`
          : '<span class="badge b-neutral">direct</span>';
        const path = t["[access_path]"];
        return (
          '<div class="route">' +
          `<span class="route-head">${perm}${viaHtml}</span>` +
          (path ? `<span class="route-path">${escapeHtml(String(path))}</span>` : "") +
          "</div>"
        );
      })
      .join("") +
    "</div>"
  );
}

/* ------------------------------------------------ data security renderers */

/** SemanticModel vs OneLake - the two places row/column rules can live. */
function planeBadge(v) {
  const s = String(v || "");
  if (!s) return '<span class="dim">—</span>';
  const cls = s === "OneLake" ? "b-build" : "b-read";
  const label = s === "SemanticModel" ? "model" : "OneLake";
  return `<span class="badge ${cls}">${escapeHtml(label)}</span>`;
}

/** Table plus, for column rules, the columns the rule names. */
function renderScope(table, columns) {
  const t = String(table || "").trim();
  const c = String(columns || "").trim();
  if (!t && !c) return '<span class="dim">whole item</span>';
  const parts = [];
  if (t) parts.push(`<span class="scope-table">${escapeHtml(t)}</span>`);
  if (c) {
    const cols = splitList(c)
      .map((x) => `<span class="chip">${escapeHtml(x)}</span>`)
      .join("");
    parts.push(`<span class="scope-cols">${cols}</span>`);
  }
  return `<div class="scope">${parts.join("")}</div>`;
}

/**
 * A one-line summary with the verbatim expression underneath.
 *
 * The expression is the thing an admin actually has to reason about, so it is
 * never truncated away - it is shown in full, in a monospace block, exactly as
 * it is defined in the model or the OneLake role.
 */
function renderRule(summary, expression) {
  const s = String(summary || "").trim();
  const e = String(expression || "").trim();
  if (!s && !e) return '<span class="dim">—</span>';
  let html = '<div class="rule">';
  if (s) html += `<span class="rule-sum">${escapeHtml(s)}</span>`;
  if (e && e !== s) html += `<code class="rule-expr">${escapeHtml(e)}</code>`;
  return html + "</div>";
}

/** Compact RLS/CLS indicator for the access tables. */
function dataSecBadges(hasRls, hasCls, roles) {
  const out = [];
  if (hasRls) out.push('<span class="badge b-build" title="Row-level security applies">RLS</span>');
  if (hasCls) out.push('<span class="badge b-reshare" title="Column-level security applies">CLS</span>');
  if (!out.length) return '<span class="dim">—</span>';
  const names = splitList(roles);
  const title = names.length ? ` title="${escapeHtml(names.join(", "))}"` : "";
  return `<span class="dsec"${title}>${out.join("")}</span>`;
}

/* --------------------------------------------------------------- widgets */

function kpi(label, value, foot, cls = "", onClick) {
  const c = el("div", `card kpi ${cls}${onClick ? " clickable" : ""}`);
  c.innerHTML =
    `<div class="label">${escapeHtml(label)}</div>` +
    `<div class="value">${value}</div>` +
    `<div class="foot">${foot || ""}</div>`;
  if (onClick) c.onclick = onClick;
  return c;
}

/**
 * Sortable, clickable data table.
 * cols: [{key, label, num, render, width}]
 */
function dataTable(rows, cols, opts = {}) {
  const wrap = el("div", "tbl-wrap");
  if (opts.maxHeight) wrap.style.setProperty("--h", opts.maxHeight);
  if (!rows || !rows.length) {
    wrap.appendChild(el("div", "empty", opts.emptyText || "No rows match the current filters."));
    return wrap;
  }

  let sortKey = opts.sortKey ?? null;
  let sortDesc = opts.sortDesc ?? true;

  const table = el("table");
  const thead = el("thead");
  const htr = el("tr");
  cols.forEach((c) => {
    const th = el("th", `sortable${c.num ? " num" : ""}`, escapeHtml(c.label));
    th.onclick = () => {
      if (sortKey === c.key) sortDesc = !sortDesc;
      else { sortKey = c.key; sortDesc = !!c.num; }
      draw();
    };
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);
  const tbody = el("tbody");
  table.appendChild(tbody);
  wrap.appendChild(table);

  function draw() {
    let data = rows.slice();
    if (sortKey) {
      const col = cols.find((c) => c.key === sortKey);
      data.sort((a, b) => {
        const av = pick(a, sortKey), bv = pick(b, sortKey);
        if (col?.num) return (sortDesc ? 1 : -1) * ((Number(bv) || 0) - (Number(av) || 0));
        return (sortDesc ? -1 : 1) * String(av ?? "").localeCompare(String(bv ?? ""));
      });
    }
    if (opts.limit) data = data.slice(0, opts.limit);

    tbody.innerHTML = "";
    data.forEach((r) => {
      const tr = el("tr", opts.onRowClick ? "clickable" : "");
      cols.forEach((c) => {
        const td = el("td", c.num ? "num" : "");
        const v = pick(r, c.key);
        td.innerHTML = c.render ? c.render(v, r) : escapeHtml(v ?? "—");
        if (c.width) td.style.maxWidth = c.width;
        tr.appendChild(td);
      });
      if (opts.onRowClick) tr.onclick = () => opts.onRowClick(r);
      tbody.appendChild(tr);
    });

    htr.querySelectorAll("th").forEach((th, i) => {
      const c = cols[i];
      th.innerHTML =
        escapeHtml(c.label) + (sortKey === c.key ? (sortDesc ? " &darr;" : " &uarr;") : "");
    });
  }
  draw();
  return wrap;
}

/** Horizontal bar list — used everywhere a "top N" breakdown is needed. */
function barList(rows, { nameKey, valueKey, onClick, format = compactNum, limit = 10 }) {
  const box = el("div");
  if (!rows || !rows.length) {
    box.appendChild(el("div", "empty", "Nothing to show."));
    return box;
  }
  const data = rows.slice(0, limit);
  const max = Math.max(...data.map((r) => Number(pick(r, valueKey)) || 0), 1);
  data.forEach((r) => {
    const name = pick(r, nameKey);
    const v = Number(pick(r, valueKey)) || 0;
    const row = el("div", `bar-row${onClick ? " clickable" : ""}`);
    row.innerHTML =
      `<span class="nm" title="${escapeHtml(name)}">${escapeHtml(name ?? "—")}</span>` +
      `<span class="track"><span class="fill" style="width:${(v / max) * 100}%"></span></span>` +
      `<span class="vl">${format(v)}</span>`;
    if (onClick) row.onclick = () => onClick(name, r);
    box.appendChild(row);
  });
  return box;
}

function card(title, sub, body, extraClass = "") {
  const c = el("div", `card ${extraClass}`);
  const head = el("div", "card-head");
  const t = el("div");
  t.innerHTML = `<h3>${escapeHtml(title)}</h3><p class="sub">${escapeHtml(sub || "")}</p>`;
  head.appendChild(t);
  c.appendChild(head);
  if (body) c.appendChild(body);
  return c;
}

function loadingCard(title) {
  return card(title, "", el("div", "loading", '<span class="spinner"></span>Querying model…'));
}

/** Replace a container's contents once an async producer resolves. */
async function fill(container, producer, label) {
  container.innerHTML = "";
  container.appendChild(el("div", "loading", '<span class="spinner"></span>Querying model…'));
  try {
    const node = await producer();
    container.innerHTML = "";
    container.appendChild(node);
  } catch (e) {
    container.innerHTML = "";
    container.appendChild(el("div", "err", `${label || "Query"} failed: ${escapeHtml(e.message)}`));
  }
}

/* ------------------------------------------------------------- chart.js */

/* Chart colours come from the stylesheet rather than being repeated here, so
 * retheming the app means editing one :root block. Fallbacks keep charts
 * readable if the stylesheet fails to load or in a headless test harness. */
function cssVar(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  } catch {
    return fallback;
  }
}

const THEME = {
  accent:  () => cssVar("--accent", "#14a67f"),
  accent2: () => cssVar("--accent-2", "#3fd9a4"),
  panel:   () => cssVar("--panel", "#0a1a18"),
  muted:   () => cssVar("--muted", "#8bb0a8"),
  border:  () => cssVar("--border", "#1d413a"),
  border2: () => cssVar("--border-2", "#2a5a4f"),
  red:     () => cssVar("--red", "#ff5e6c"),
  amber:   () => cssVar("--amber", "#ffc861"),
  dim:     () => cssVar("--dim", "#5f857d"),
  series:  () => CHART_COLORS,
};

const CHART_FALLBACK = [
  "#14a67f", "#4fd1a6", "#ffb454", "#9b8cff",
  "#ff6b9d", "#6fe3ff", "#ff5e6c", "#6b8a84",
];
const CHART_COLORS = CHART_FALLBACK.map((f, i) => cssVar(`--c${i + 1}`, f));

/** Hex -> rgba(), for fills that need the theme colour at low opacity. */
function withAlpha(hex, a) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim());
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

const _charts = {};

function drawChart(canvasId, cfg) {
  const c = document.getElementById(canvasId);
  if (!c) return;
  if (_charts[canvasId]) _charts[canvasId].destroy();
  Chart.defaults.color = THEME.muted();
  Chart.defaults.borderColor = withAlpha(THEME.border(), 0.6);
  Chart.defaults.font.family = '"Segoe UI",system-ui,sans-serif';
  _charts[canvasId] = new Chart(c, cfg);
  return _charts[canvasId];
}
