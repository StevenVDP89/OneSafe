/* OneSafe panes: Overview, Principal 360, Item 360, Access Graph,
 * OneLake Security, Risk & Drift, Compare, Health.
 *
 * Every pane is a plain async function returning nothing; it writes directly
 * into its container. Panes read the shared filter state so a click anywhere
 * cross-filters everywhere.
 */

const PANES = {};
let activePane = "overview";

function refreshActivePane() {
  const fn = PANES[activePane];
  if (fn) fn($("paneBody"));
}

function showPane(name) {
  activePane = name;
  document.querySelectorAll("nav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.pane === name)
  );
  refreshActivePane();
}

/* Shared bits ------------------------------------------------------------ */

const ACCESS_FILTER_KEYS = [
  "principal", "principalId", "ptype", "workspace", "itemType", "item",
  "itemId", "capacity", "permission", "source", "risk", "viaGroup", "restricted",
];

/** SUMMARIZECOLUMNS wrapped in the global filter context. */
function scoped(groupCols, measures, exclude = []) {
  const parts = [snapshotExpr(), ...filterExprs(exclude)];
  const inner =
    `SUMMARIZECOLUMNS(\n  ${groupCols.join(",\n  ")},\n  ${measures.join(",\n  ")}\n)`;
  return `EVALUATE\nCALCULATETABLE(\n${inner}${parts.map((p) => ",\n  " + p).join("")}\n)`;
}

/** Single-row measure query. */
function scalars(measures, exclude = []) {
  const parts = [snapshotExpr(), ...filterExprs(exclude)];
  return `EVALUATE\nCALCULATETABLE(\n  ROW(${measures.join(", ")})${parts
    .map((p) => ",\n  " + p)
    .join("")}\n)`;
}

function section(title, sub) {
  const h = el("div", "sec");
  h.innerHTML =
    `<div class="sec-title">${escapeHtml(title)}</div>` +
    (sub ? `<div class="sec-sub">${escapeHtml(sub)}</div>` : "");
  return h;
}

/* ===================================================================== */
/* 1. OVERVIEW                                                            */
/* ===================================================================== */

PANES.overview = async function (root) {
  root.innerHTML = "";

  const kpiRow = el("div", "grid g5");
  root.appendChild(kpiRow);
  const riskRow = el("div", "grid g4");
  riskRow.style.marginTop = "14px";
  root.appendChild(riskRow);

  root.appendChild(section("Where access concentrates", "Click any bar to cross-filter the whole app."));
  const mid = el("div", "grid g3");
  root.appendChild(mid);

  root.appendChild(section("How access is granted"));
  const low = el("div", "grid g-2-1");
  root.appendChild(low);

  /* --- KPI strip */
  (async () => {
    try {
      const r = await runDax(
        scalars([
          '"Principals", [Principals with Access]',
          '"Items", [Items Accessible]',
          '"Workspaces", [Workspaces Accessible]',
          '"Paths", [Access Paths]',
          '"Pairs", [Principal-Item Pairs]',
          '"Inherited", [Inherited Access %]',
          '"Multi", [Multi-Path %]',
        ])
      );
      kpiRow.innerHTML = "";
      kpiRow.appendChild(kpi("Principals with access", nf(val(r, "[Principals]")),
        "users, groups, SPNs and managed identities"));
      kpiRow.appendChild(kpi("Items reachable", nf(val(r, "[Items]")),
        `across ${nf(val(r, "[Workspaces]"))} workspaces`));
      kpiRow.appendChild(kpi("Access paths", nf(val(r, "[Paths]")),
        `${nf(val(r, "[Pairs]"))} distinct principal-item pairs`));
      kpiRow.appendChild(kpi("Granted via group", pctFmt(val(r, "[Inherited]")),
        "of all paths flow through a group", "warn",
        () => setFilter("viaGroup", state.filters.viaGroup ? null : true)));
      kpiRow.appendChild(kpi("Multiple routes", pctFmt(val(r, "[Multi]")),
        "pairs reachable more than one way"));
    } catch (e) {
      kpiRow.innerHTML = "";
      kpiRow.appendChild(el("div", "err", "KPIs failed: " + escapeHtml(e.message)));
    }
  })();

  /* --- risk tiles */
  (async () => {
    try {
      const r = await runDax(
        scalars([
          '"Risk", [Risk Paths]',
          '"RiskPct", [Risk Path %]',
          '"Guest", [Guest Access Paths]',
          '"Orphan", [Orphaned Access Paths]',
          '"SpnWrite", [Service Principal Write Paths]',
          '"Reshare", [Item Reshare Paths]',
          '"Broad", [Broad Group Paths]',
          '"RiskyPpl", [Risky Principals]',
        ])
      );
      riskRow.innerHTML = "";
      const tiles = [
        ["Risky access paths", nf(val(r, "[Risk]")), `${pctFmt(val(r, "[RiskPct]"))} of all paths · ${nf(val(r, "[RiskyPpl]"))} principals`, "risk", null],
        ["Reshare privilege", nf(val(r, "[Reshare]")), "can re-grant access to others", "warn", "ItemResharePrivilege"],
        ["Orphaned principals", nf(val(r, "[Orphan]")), "disabled or unresolvable, still granted", "risk", "OrphanedPrincipal"],
        ["Service principal write", nf(val(r, "[SpnWrite]")), "non-human identities with write", "warn", "ServicePrincipalWriteAccess"],
      ];
      tiles.forEach(([l, v, f, c, flag]) =>
        riskRow.appendChild(kpi(l, v, f, c, flag ? () => setFilter("risk", state.filters.risk === flag ? null : flag) : null))
      );
      const extra = el("div", "grid g4");
      extra.style.marginTop = "14px";
      [
        ["Guest access", nf(val(r, "[Guest]")), "external identities", "warn", "GuestAccess"],
        ["Broad group grants", nf(val(r, "[Broad]")), "grants via very large groups", "warn", "BroadGroupGrant"],
      ].forEach(([l, v, f, c, flag]) =>
        extra.appendChild(kpi(l, v, f, c, () => setFilter("risk", state.filters.risk === flag ? null : flag)))
      );
    } catch (e) {
      riskRow.innerHTML = "";
      riskRow.appendChild(el("div", "err", "Risk tiles failed: " + escapeHtml(e.message)));
    }
  })();

  /* --- top breakdowns */
  const c1 = el("div"), c2 = el("div"), c3 = el("div");
  mid.append(c1, c2, c3);

  fill(c1, async () => {
    const rows = await runDax(
      scoped(["'dim_principal'[display_name]", "'dim_principal'[principal_type]"],
        ['"paths", [Access Paths]', '"items", [Items Accessible]'], ["principal", "principalId"])
    );
    rows.sort((a, b) => (pick(b, "[paths]") || 0) - (pick(a, "[paths]") || 0));
    return card("Widest reach", "Principals with the most access paths",
      barList(rows, {
        nameKey: "[display_name]", valueKey: "[paths]", limit: 12,
        onClick: (n) => setFilter("principal", n),
      }));
  }, "Top principals");

  fill(c2, async () => {
    const rows = await runDax(
      scoped(["'dim_workspace'[workspace_name]"],
        ['"paths", [Access Paths]', '"ppl", [Principals with Access]'], ["workspace"])
    );
    rows.sort((a, b) => (pick(b, "[paths]") || 0) - (pick(a, "[paths]") || 0));
    return card("Most exposed workspaces", "By number of access paths into their items",
      barList(rows, {
        nameKey: "[workspace_name]", valueKey: "[paths]", limit: 12,
        onClick: (n) => setFilter("workspace", n),
      }));
  }, "Top workspaces");

  fill(c3, async () => {
    const rows = await runDax(
      scoped(["'dim_item'[item_type]"], ['"paths", [Access Paths]'], ["itemType"])
    );
    rows.sort((a, b) => (pick(b, "[paths]") || 0) - (pick(a, "[paths]") || 0));
    return card("Item types", "Which kinds of artifact carry the access",
      barList(rows, {
        nameKey: "[item_type]", valueKey: "[paths]", limit: 12,
        onClick: (n) => setFilter("itemType", n),
      }));
  }, "Item types");

  /* --- grant source + principal type */
  const c4 = el("div"), c5 = el("div");
  low.append(c4, c5);

  fill(c4, async () => {
    const rows = await runDax(
      scoped(["'fact_effective_access'[grant_source]", "'fact_effective_access'[permission_name]"],
        ['"paths", [Access Paths]'], ["source", "permission"])
    );
    const sources = [...new Set(rows.map((r) => pick(r, "[grant_source]")))];
    const perms = ["Read", "Build", "Explore", "Write", "Reshare", "Admin", "Owner"]
      .filter((p) => rows.some((r) => pick(r, "[permission_name]") === p));

    const body = el("div");
    const box = el("div", "chart-box");
    box.style.setProperty("--ch", "270px");
    box.innerHTML = '<canvas id="ovGrant"></canvas>';
    body.appendChild(box);

    const c = card("Grant source by permission",
      "Workspace roles inherit down to every item; direct grants target one artifact", body);
    setTimeout(() => {
      drawChart("ovGrant", {
        type: "bar",
        data: {
          labels: sources,
          datasets: perms.map((p, i) => ({
            label: p,
            backgroundColor: CHART_COLORS[i % CHART_COLORS.length],
            data: sources.map((s) =>
              rows.filter((r) => pick(r, "[grant_source]") === s && pick(r, "[permission_name]") === p)
                .reduce((a, r) => a + (Number(pick(r, "[paths]")) || 0), 0)
            ),
          })),
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true } },
          plugins: { legend: { position: "bottom", labels: { boxWidth: 11, font: { size: 11 } } } },
          onClick: (evt, els2, chart) => {
            if (!els2.length) return;
            setFilter("source", sources[els2[0].index]);
          },
        },
      });
    }, 0);
    return c;
  }, "Grant sources");

  fill(c5, async () => {
    const rows = await runDax(
      scoped(["'dim_principal'[principal_type]"], ['"paths", [Access Paths]', '"n", [Principals with Access]'], ["ptype"])
    );
    const body = el("div");
    const box = el("div", "chart-box");
    box.style.setProperty("--ch", "230px");
    box.innerHTML = '<canvas id="ovType"></canvas>';
    body.appendChild(box);
    const c = card("Identity mix", "Who holds the access", body);
    setTimeout(() => {
      const labels = rows.map((r) => pick(r, "[principal_type]") || "Unknown");
      drawChart("ovType", {
        type: "doughnut",
        data: {
          labels,
          datasets: [{
            data: rows.map((r) => Number(pick(r, "[paths]")) || 0),
            backgroundColor: CHART_COLORS,
            borderColor: THEME.panel(), borderWidth: 2,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false, cutout: "62%",
          plugins: { legend: { position: "bottom", labels: { boxWidth: 11, font: { size: 11 } } } },
          onClick: (e, els2) => { if (els2.length) setFilter("ptype", labels[els2[0].index]); },
        },
      });
    }, 0);
    return c;
  }, "Identity mix");

  /* --- detail table */
  root.appendChild(section("Access detail", "Every resolved path under the current filters"));
  const det = el("div");
  root.appendChild(det);
  fill(det, async () => {
    const raw = await runDax(
      scoped(
        ["'dim_principal'[display_name]", "'dim_principal'[principal_type]",
         "'dim_item'[item_name]", "'dim_item'[item_type]", "'dim_item'[workspace_name]",
         "'fact_effective_access'[permission_name]", "'fact_effective_access'[grant_source]",
         "'fact_effective_access'[access_path]", "'fact_effective_access'[risk_flags]"],
        ['"paths", [Access Paths]']
      )
    );
    const rows = collapseRows(raw,
      ["[display_name]", "[principal_type]", "[workspace_name]", "[item_name]", "[item_type]"], {
        merge: ["[permission_name]", "[risk_flags]"],
        sum: ["[paths]"],
        pair: { key: "[grant_routes]", cols: ["[permission_name]", "[grant_source]", "[access_path]"] },
      });
    return card(`Who can reach what (${nf(rows.length)})`,
      `Resolved from ${nf(raw.length)} grant route(s) — click a row to pivot to that principal`,
      dataTable(rows, [
        { key: "[display_name]", label: "Principal" },
        { key: "[principal_type]", label: "Type", render: (v) => `<span class="badge b-neutral">${escapeHtml(v)}</span>` },
        { key: "[workspace_name]", label: "Workspace" },
        { key: "[item_name]", label: "Item" },
        { key: "[item_type]", label: "Item type" },
        { key: "[permission_name]", label: "Effective", render: permBadges },
        { key: "[grant_routes]", label: "How access is granted", render: renderGrantRoutes, width: "38%" },
        { key: "[risk_flags]", label: "Risk", render: riskBadges },
      ], {
        maxHeight: "540px", limit: 800, sortKey: "[display_name]", sortDesc: false,
        onRowClick: (r) => { setFilter("principal", pick(r, "[display_name]")); showPane("principal"); },
      }));
  }, "Access detail");
};

/* ===================================================================== */
/* 2. PRINCIPAL 360                                                       */
/* ===================================================================== */

let _principalCache = null;

async function loadPrincipals() {
  if (_principalCache) return _principalCache;
  _principalCache = await runDax(
    `EVALUATE
CALCULATETABLE(
  SUMMARIZECOLUMNS(
    'dim_principal'[principal_id],
    'dim_principal'[display_name],
    'dim_principal'[upn],
    'dim_principal'[principal_type],
    'dim_principal'[is_orphaned],
    "paths", [Access Paths],
    "items", [Items Accessible]
  ),
  ${snapshotExpr()}
)`,
    { cache: false }
  );
  return _principalCache;
}

function searchBox(placeholder, loader, onPick, renderRow) {
  const wrap = el("div", "suggest");
  wrap.style.flex = "1";
  const inp = el("input");
  inp.placeholder = placeholder;
  inp.autocomplete = "off";
  wrap.appendChild(inp);
  const list = el("div", "suggest-list");
  list.style.display = "none";
  wrap.appendChild(list);

  let data = null;
  async function ensure() {
    if (!data) { inp.placeholder = "Loading…"; data = await loader(); inp.placeholder = placeholder; }
    return data;
  }
  async function show() {
    const d = await ensure();
    const term = inp.value.trim().toLowerCase();
    const hits = d
      .filter((r) => !term || renderRow.text(r).toLowerCase().includes(term))
      .slice(0, 60);
    list.innerHTML = "";
    if (!hits.length) { list.style.display = "none"; return; }
    hits.forEach((r) => {
      const d2 = el("div", "", renderRow.html(r));
      d2.onclick = () => { list.style.display = "none"; inp.value = ""; onPick(r); };
      list.appendChild(d2);
    });
    list.style.display = "block";
  }
  inp.oninput = show;
  inp.onfocus = show;
  inp.onblur = () => setTimeout(() => (list.style.display = "none"), 180);
  return wrap;
}

PANES.principal = async function (root) {
  root.innerHTML = "";

  const bar = el("div", "search-row");
  bar.appendChild(
    searchBox("Search any user, group, service principal or managed identity…", loadPrincipals,
      (r) => {
        state.filters.principalId = pick(r, "[principal_id]");
        state.filters.principal = pick(r, "[display_name]");
        _cacheEpoch++;
        renderFilterBar();
        refreshActivePane();
      },
      {
        text: (r) => `${pick(r, "[display_name]") || ""} ${pick(r, "[upn]") || ""} ${pick(r, "[principal_type]") || ""}`,
        html: (r) =>
          `<div>${escapeHtml(pick(r, "[display_name]") || "(unnamed)")} ` +
          `<span class="badge b-neutral">${escapeHtml(pick(r, "[principal_type]"))}</span>` +
          (pick(r, "[is_orphaned]") ? ' <span class="badge b-risk">orphaned</span>' : "") +
          `</div><div class="meta">${escapeHtml(pick(r, "[upn]") || "no UPN")} · ` +
          `${nf(pick(r, "[paths]"))} paths · ${nf(pick(r, "[items]"))} items</div>`,
      })
  );
  root.appendChild(bar);

  const pid = state.filters.principalId;
  const pname = state.filters.principal;
  if (!pid && !pname) {
    root.appendChild(card("Pick a principal", "",
      el("div", "empty",
        "Search above, or click a principal anywhere else in OneSafe to bring them here.")));
    // Still show the leaderboard so the pane is useful cold.
    const lb = el("div");
    lb.style.marginTop = "14px";
    root.appendChild(lb);
    fill(lb, async () => {
      const rows = await loadPrincipals();
      rows.sort((a, b) => (pick(b, "[paths]") || 0) - (pick(a, "[paths]") || 0));
      return card("All principals", `${nf(rows.length)} identities hold at least one grant`,
        dataTable(rows, [
          { key: "[display_name]", label: "Principal" },
          { key: "[upn]", label: "UPN / app id" },
          { key: "[principal_type]", label: "Type", render: (v) => `<span class="badge b-neutral">${escapeHtml(v)}</span>` },
          { key: "[is_orphaned]", label: "State", render: (v) => v ? '<span class="badge b-risk">orphaned</span>' : '<span class="badge b-ok">active</span>' },
          { key: "[items]", label: "Items", num: true, render: nf },
          { key: "[paths]", label: "Paths", num: true, render: nf },
        ], {
          maxHeight: "520px", sortKey: "[paths]",
          onRowClick: (r) => {
            state.filters.principalId = pick(r, "[principal_id]");
            state.filters.principal = pick(r, "[display_name]");
            _cacheEpoch++; renderFilterBar(); refreshActivePane();
          },
        }));
    }, "Principals");
    return;
  }

  /* header card */
  const hdr = el("div");
  root.appendChild(hdr);
  fill(hdr, async () => {
    const r = await runDax(
      scalars([
        '"ws", [Workspaces Accessible]',
        '"items", [Items Accessible]',
        '"paths", [Access Paths]',
        '"maxp", [Max Permission Level]',
        '"risk", [Risk Paths]',
        '"restricted", [Data-Plane Restricted Paths]',
        '"direct", [Direct Access %]',
      ])
    );
    const g = el("div", "grid g5");
    g.appendChild(kpi("Workspaces", nf(val(r, "[ws]")), "reachable"));
    g.appendChild(kpi("Items", nf(val(r, "[items]")), "artifacts reachable"));
    g.appendChild(kpi("Access paths", nf(val(r, "[paths]")), `${pctFmt(val(r, "[direct]"))} granted directly`));
    g.appendChild(kpi("Highest permission", ["None","Read","Build","Write","Reshare","Admin"][val(r, "[maxp]")] || "None",
      "strongest right held anywhere", val(r, "[maxp]") >= 4 ? "risk" : ""));
    g.appendChild(kpi("Risky paths", nf(val(r, "[risk]")),
      `${nf(val(r, "[restricted]"))} constrained by OneLake`, val(r, "[risk]") ? "risk" : "good"));
    return g;
  }, "Principal summary");

  /* workspaces + groups */
  root.appendChild(section("Reach", "Where this identity can go, and how it got there"));
  const row = el("div", "grid g3");
  root.appendChild(row);
  const w1 = el("div"), w2 = el("div"), w3 = el("div");
  row.append(w1, w2, w3);

  fill(w1, async () => {
    const rows = await runDax(
      scoped(["'dim_item'[workspace_name]"], ['"paths", [Access Paths]', '"items", [Items Accessible]'])
    );
    rows.sort((a, b) => (pick(b, "[items]") || 0) - (pick(a, "[items]") || 0));
    return card("Workspaces", `${rows.length} reachable`,
      barList(rows, {
        nameKey: "[workspace_name]", valueKey: "[items]", limit: 14,
        onClick: (n) => setFilter("workspace", n),
      }));
  }, "Workspaces");

  fill(w2, async () => {
    const rows = await runDax(
      scoped(["'fact_effective_access'[permission_name]", "'fact_effective_access'[grant_source]"],
        ['"paths", [Access Paths]'], ["permission", "source"])
    );
    return card("Permissions held", "By the route that granted them",
      dataTable(rows, [
        { key: "[permission_name]", label: "Permission", render: permBadge },
        { key: "[grant_source]", label: "Granted by" },
        { key: "[paths]", label: "Paths", num: true, render: nf },
      ], { maxHeight: "330px", sortKey: "[paths]" }));
  }, "Permissions");

  fill(w3, async () => {
    if (!pid) return card("Group memberships", "", el("div", "empty", "Select a principal by name to resolve groups."));
    const rows = await runDax(
      `EVALUATE
SELECTCOLUMNS(
  FILTER(
    CALCULATETABLE('bridge_group_membership', ${snapshotExpr()}),
    'bridge_group_membership'[principal_id] = "${q(pid)}"
  ),
  "group_id", 'bridge_group_membership'[group_id],
  "group_name", LOOKUPVALUE('dim_principal'[display_name],
      'dim_principal'[principal_id], 'bridge_group_membership'[group_id]),
  "member_type", 'bridge_group_membership'[member_type]
)`
    );
    return card("Group memberships", `Inherits access from ${rows.length} group(s)`,
      rows.length
        ? dataTable(rows, [
            { key: "[group_name]", label: "Group", render: (v) => `<span class="badge b-group">${escapeHtml(v || "(unresolved)")}</span>` },
            { key: "[member_type]", label: "Membership" },
          ], {
            maxHeight: "330px",
            onRowClick: (r) => {
              const gid = pick(r, "[group_id]"), gn = pick(r, "[group_name]");
              if (!gn) return;
              state.filters.principalId = gid; state.filters.principal = gn;
              _cacheEpoch++; renderFilterBar(); refreshActivePane();
            },
          })
        : el("div", "empty", "No group memberships — all access is granted directly."));
  }, "Groups");

  /* full access table */
  root.appendChild(section("Every item this identity can reach"));
  const det = el("div");
  root.appendChild(det);
  fill(det, async () => {
    const raw = await runDax(
      scoped(
        ["'dim_item'[workspace_name]", "'dim_item'[item_name]", "'dim_item'[item_type]",
         "'fact_effective_access'[permission_name]", "'fact_effective_access'[grant_source]",
         "'fact_effective_access'[granted_via_name]", "'fact_effective_access'[access_path]",
         "'fact_effective_access'[data_plane_restricted]",
         "'fact_effective_access'[has_rls]", "'fact_effective_access'[has_cls]",
         "'fact_effective_access'[data_security_roles]",
         "'fact_effective_access'[risk_flags]"],
        ['"paths", [Access Paths]']
      )
    );
    // One row per item, not per route: an identity holding both Admin and
    // Reshare on the same pipeline is one thing to review, not two.
    const rows = collapseRows(raw, ["[workspace_name]", "[item_name]", "[item_type]"], {
      merge: ["[permission_name]", "[risk_flags]", "[data_security_roles]"],
      sum: ["[paths]"],
      any: ["[data_plane_restricted]", "[has_rls]", "[has_cls]"],
      pair: { key: "[grant_routes]", cols: ["[permission_name]", "[granted_via_name]", "[access_path]"] },
    });
    return card(`Items reachable (${nf(rows.length)})`,
      `Across ${nf(raw.length)} distinct grant route(s) — click a row to inspect the item`,
      dataTable(rows, [
        { key: "[workspace_name]", label: "Workspace" },
        { key: "[item_name]", label: "Item" },
        { key: "[item_type]", label: "Type" },
        { key: "[permission_name]", label: "Effective", render: permBadges },
        { key: "[grant_routes]", label: "How access is granted", render: renderGrantRoutes, width: "40%" },
        // Permission says they can open it. This says how much of it they see.
        {
          key: "[has_rls]", label: "Row/col",
          render: (v, r) => dataSecBadges(v, pick(r, "[has_cls]"), pick(r, "[data_security_roles]")),
        },
        { key: "[data_plane_restricted]", label: "OneLake", render: (v) => v ? '<span class="badge b-reshare">restricted</span>' : '<span class="badge b-neutral">full</span>' },
        { key: "[risk_flags]", label: "Risk", render: riskBadges },
      ], {
        maxHeight: "560px", sortKey: "[workspace_name]", sortDesc: false,
        onRowClick: (r) => { setFilter("item", pick(r, "[item_name]")); showPane("item"); },
      }));
  }, "Access paths");

  /* onelake + rls for this principal */
  const bot = el("div", "grid g2");
  bot.style.marginTop = "14px";
  root.appendChild(bot);
  const b1 = el("div"), b2 = el("div");
  bot.append(b1, b2);

  fill(b1, async () => {
    if (!pid) return card("OneLake data access", "", el("div", "empty", "Select a principal."));
    const rows = await runDax(
      `EVALUATE
SELECTCOLUMNS(
  FILTER(
    CALCULATETABLE('fact_onelake_role_member', ${snapshotExpr()}),
    'fact_onelake_role_member'[principal_id] = "${q(pid)}"
  ),
  "role_name", LOOKUPVALUE('fact_onelake_role'[role_name],
      'fact_onelake_role'[role_id], 'fact_onelake_role_member'[role_id],
      'fact_onelake_role'[snapshot_date], 'fact_onelake_role_member'[snapshot_date]),
  "item_name", LOOKUPVALUE('dim_item'[item_name],
      'dim_item'[item_id], 'fact_onelake_role_member'[item_id]),
  "source_type", 'fact_onelake_role_member'[source_type],
  "source_path", 'fact_onelake_role_member'[source_path]
)`
    );
    return card("OneLake data access roles",
      "Data-plane membership, which constrains what rows and folders are actually readable",
      rows.length
        ? dataTable(rows, [
            { key: "[item_name]", label: "Lakehouse" },
            { key: "[role_name]", label: "Role" },
            { key: "[source_type]", label: "Source" },
            { key: "[source_path]", label: "Path" },
          ], { maxHeight: "300px" })
        : el("div", "empty", "Not a member of any OneLake data access role. Access to data is governed by item permissions alone."));
  }, "OneLake");

  fill(b2, async () => {
    if (!pid) return card("Row-level security", "", el("div", "empty", "Select a principal."));
    const rows = await runDax(
      `EVALUATE
SELECTCOLUMNS(
  FILTER(
    CALCULATETABLE('fact_rls_role_member', ${snapshotExpr()}),
    'fact_rls_role_member'[principal_id] = "${q(pid)}"
  ),
  "item_name", LOOKUPVALUE('dim_item'[item_name],
      'dim_item'[item_id], 'fact_rls_role_member'[item_id]),
  "rls_role", 'fact_rls_role_member'[rls_role],
  "member_type", 'fact_rls_role_member'[member_type],
  "table_count", 'fact_rls_role_member'[table_count]
)`
    );
    return card("Semantic model row-level security",
      "RLS roles narrow what this identity sees inside a model, even with Read or Build",
      rows.length
        ? dataTable(rows, [
            { key: "[item_name]", label: "Semantic model" },
            { key: "[rls_role]", label: "RLS role" },
            { key: "[member_type]", label: "Member as" },
            { key: "[table_count]", label: "Filtered tables", num: true },
          ], { maxHeight: "300px" })
        : el("div", "empty", "No RLS role membership. Any model this identity can read is read in full."));
  }, "RLS");
};

/* ===================================================================== */
/* 3. ITEM 360                                                            */
/* ===================================================================== */

let _itemCache = null;
async function loadItems() {
  if (_itemCache) return _itemCache;
  _itemCache = await runDax(
    `EVALUATE
CALCULATETABLE(
  SUMMARIZECOLUMNS(
    'dim_item'[item_id],
    'dim_item'[item_name],
    'dim_item'[item_type],
    'dim_item'[workspace_name],
    'dim_item'[has_onelake_security],
    "ppl", [Principals with Access],
    "paths", [Access Paths]
  ),
  ${snapshotExpr()}
)`,
    { cache: false }
  );
  return _itemCache;
}

PANES.item = async function (root) {
  root.innerHTML = "";

  const bar = el("div", "search-row");
  bar.appendChild(
    searchBox("Search any workspace item — report, semantic model, lakehouse, notebook…", loadItems,
      (r) => {
        state.filters.itemId = pick(r, "[item_id]");
        state.filters.item = pick(r, "[item_name]");
        _cacheEpoch++; renderFilterBar(); refreshActivePane();
      },
      {
        text: (r) => `${pick(r, "[item_name]") || ""} ${pick(r, "[item_type]") || ""} ${pick(r, "[workspace_name]") || ""}`,
        html: (r) =>
          `<div>${escapeHtml(pick(r, "[item_name]"))} <span class="badge b-neutral">${escapeHtml(pick(r, "[item_type]"))}</span>` +
          (pick(r, "[has_onelake_security]") ? ' <span class="badge b-reshare">OneLake</span>' : "") +
          `</div><div class="meta">${escapeHtml(pick(r, "[workspace_name]"))} · ${nf(pick(r, "[ppl]"))} principals</div>`,
      })
  );
  root.appendChild(bar);

  const iid = state.filters.itemId, iname = state.filters.item;
  if (!iid && !iname) {
    const lb = el("div");
    root.appendChild(lb);
    fill(lb, async () => {
      const rows = await loadItems();
      rows.sort((a, b) => (pick(b, "[ppl]") || 0) - (pick(a, "[ppl]") || 0));
      return card("All items", `${nf(rows.length)} artifacts with at least one grant · sorted by exposure`,
        dataTable(rows, [
          { key: "[item_name]", label: "Item" },
          { key: "[item_type]", label: "Type", render: (v) => `<span class="badge b-neutral">${escapeHtml(v)}</span>` },
          { key: "[workspace_name]", label: "Workspace" },
          { key: "[has_onelake_security]", label: "OneLake security", render: (v) => v ? '<span class="badge b-reshare">enabled</span>' : '<span class="badge b-neutral">none</span>' },
          { key: "[ppl]", label: "Principals", num: true, render: nf },
          { key: "[paths]", label: "Paths", num: true, render: nf },
        ], {
          maxHeight: "560px", sortKey: "[ppl]",
          onRowClick: (r) => {
            state.filters.itemId = pick(r, "[item_id]");
            state.filters.item = pick(r, "[item_name]");
            _cacheEpoch++; renderFilterBar(); refreshActivePane();
          },
        }));
    }, "Items");
    return;
  }

  const hdr = el("div");
  root.appendChild(hdr);
  fill(hdr, async () => {
    const r = await runDax(
      scalars([
        '"ppl", [Principals with Access]',
        '"paths", [Access Paths]',
        '"risk", [Risk Paths]',
        '"inh", [Inherited Access %]',
        '"restricted", [Data-Plane Restricted Paths]',
      ])
    );
    const g = el("div", "grid g5");
    g.appendChild(kpi("Principals with access", nf(val(r, "[ppl]")), "can reach this item"));
    g.appendChild(kpi("Access paths", nf(val(r, "[paths]")), "distinct routes in"));
    g.appendChild(kpi("Via group", pctFmt(val(r, "[inh]")), "of paths are group-inherited", "warn"));
    g.appendChild(kpi("Risky paths", nf(val(r, "[risk]")), "flagged for review", val(r, "[risk]") ? "risk" : "good"));
    g.appendChild(kpi("OneLake constrained", nf(val(r, "[restricted]")), "paths limited at the data plane"));
    return g;
  }, "Item summary");

  root.appendChild(section("Who can reach it, and how"));
  const det = el("div");
  root.appendChild(det);
  fill(det, async () => {
    const raw = await runDax(
      scoped(
        ["'dim_principal'[display_name]", "'dim_principal'[principal_type]",
         "'dim_principal'[upn]", "'dim_principal'[is_orphaned]",
         "'fact_effective_access'[permission_name]", "'fact_effective_access'[grant_source]",
         "'fact_effective_access'[granted_via_name]", "'fact_effective_access'[access_path]",
         "'fact_effective_access'[has_rls]", "'fact_effective_access'[has_cls]",
         "'fact_effective_access'[data_security_roles]",
         "'fact_effective_access'[risk_flags]"],
        ['"paths", [Access Paths]']
      )
    );
    const rows = collapseRows(raw, ["[display_name]", "[principal_type]", "[upn]"], {
      merge: ["[permission_name]", "[risk_flags]", "[data_security_roles]"],
      sum: ["[paths]"],
      any: ["[is_orphaned]", "[has_rls]", "[has_cls]"],
      pair: { key: "[grant_routes]", cols: ["[permission_name]", "[granted_via_name]", "[access_path]"] },
    });
    return card(`Principals with access (${nf(rows.length)})`,
      `Via ${nf(raw.length)} grant route(s) — click a row to pivot to that principal`,
      dataTable(rows, [
        { key: "[display_name]", label: "Principal" },
        { key: "[principal_type]", label: "Type", render: (v) => `<span class="badge b-neutral">${escapeHtml(v)}</span>` },
        { key: "[upn]", label: "UPN" },
        { key: "[permission_name]", label: "Effective", render: permBadges },
        { key: "[grant_routes]", label: "How access is granted", render: renderGrantRoutes, width: "38%" },
        {
          key: "[has_rls]", label: "Row/col",
          render: (v, r) => dataSecBadges(v, pick(r, "[has_cls]"), pick(r, "[data_security_roles]")),
        },
        { key: "[is_orphaned]", label: "State", render: (v) => v ? '<span class="badge b-risk">orphaned</span>' : '<span class="badge b-ok">active</span>' },
        { key: "[risk_flags]", label: "Risk", render: riskBadges },
      ], {
        maxHeight: "520px", sortKey: "[display_name]", sortDesc: false,
        onRowClick: (r) => { setFilter("principal", pick(r, "[display_name]")); showPane("principal"); },
      }));
  }, "Item access");

  /* item-specific security detail */
  const bot = el("div", "grid g2");
  bot.style.marginTop = "14px";
  root.appendChild(bot);
  const b1 = el("div"), b2 = el("div");
  bot.append(b1, b2);

  fill(b1, async () => {
    if (!iid) return card("OneLake security", "", el("div", "empty", "Select a single item."));
    const roles = await runDax(
      `EVALUATE
SELECTCOLUMNS(
  FILTER(CALCULATETABLE('fact_onelake_rule', ${snapshotExpr()}),
         'fact_onelake_rule'[item_id] = "${q(iid)}"),
  "role_name", LOOKUPVALUE('fact_onelake_role'[role_name],
      'fact_onelake_role'[role_id], 'fact_onelake_rule'[role_id],
      'fact_onelake_role'[snapshot_date], 'fact_onelake_rule'[snapshot_date]),
  "effect", 'fact_onelake_rule'[effect],
  "path", 'fact_onelake_rule'[path],
  "permissions", 'fact_onelake_rule'[permissions]
)`
    );
    return card("OneLake security rules", "Path and permission scoping applied at the data plane",
      roles.length
        ? dataTable(roles, [
            { key: "[role_name]", label: "Role" },
            { key: "[effect]", label: "Effect", render: (v) => `<span class="badge ${String(v).toLowerCase() === "permit" ? "b-ok" : "b-risk"}">${escapeHtml(v)}</span>` },
            { key: "[path]", label: "Path" },
            { key: "[permissions]", label: "Permissions" },
          ], { maxHeight: "300px" })
        : el("div", "empty", "No OneLake security roles on this item — anyone with item access reads all of its data."));
  }, "OneLake rules");

  fill(b2, async () => {
    if (!iid) return card("Row & column security", "", el("div", "empty", "Select a single item."));
    // Both planes at once: a semantic model's RLS/CLS roles and a lakehouse's
    // OneLake row/column constraints answer the same question for an admin.
    const rows = await runDax(
      `EVALUATE
SELECTCOLUMNS(
  FILTER(CALCULATETABLE('fact_data_security', ${snapshotExpr()}),
         'fact_data_security'[item_id] = "${q(iid)}"),
  "plane", 'fact_data_security'[plane],
  "role_name", 'fact_data_security'[role_name],
  "rule_type", 'fact_data_security'[rule_type],
  "scope_table", 'fact_data_security'[scope_table],
  "scope_column", 'fact_data_security'[scope_column],
  "rule_summary", 'fact_data_security'[rule_summary],
  "rule_expression", 'fact_data_security'[rule_expression],
  "is_dynamic", 'fact_data_security'[is_dynamic],
  "has_member", 'fact_data_security'[has_member],
  "principal_upn", 'fact_data_security'[principal_upn]
)`
    );
    return card(`Row & column security (${nf(rows.length)})`,
      "What each identity actually sees once they open this item",
      rows.length
        ? dataTable(rows, [
            { key: "[plane]", label: "Plane", render: planeBadge },
            { key: "[role_name]", label: "Role" },
            {
              key: "[rule_type]", label: "Type",
              render: (v, r) => {
                const base = String(v) === "CLS"
                  ? '<span class="badge b-reshare">column</span>'
                  : '<span class="badge b-build">row</span>';
                return pick(r, "[is_dynamic]") ? base + ' <span class="badge b-warn">dynamic</span>' : base;
              },
            },
            { key: "[scope_table]", label: "Scope", render: (v, r) => renderScope(v, pick(r, "[scope_column]")) },
            { key: "[rule_summary]", label: "Rule", render: (v, r) => renderRule(v, pick(r, "[rule_expression]")) },
            {
              key: "[principal_upn]", label: "Applies to",
              render: (v, r) => v
                ? escapeHtml(v)
                : (pick(r, "[has_member]") ? '<span class="dim">unresolved</span>'
                  : '<span class="badge b-warn">no members</span>'),
            },
          ], {
            maxHeight: "300px",
            onRowClick: (r) => {
              const u = pick(r, "[principal_upn]");
              if (u) { setFilter("principal", u); showPane("principal"); }
            },
          })
        : el("div", "empty",
            "No row- or column-level security on this item — everyone with access reads every row and column."));
  }, "Row & column security");
};
