/* OneSafe panes, part 2: Access Graph, OneLake Security, Risk & Drift,
 * Compare, Health. */

/* ===================================================================== */
/* 4. ACCESS GRAPH                                                        */
/* ===================================================================== */

/* A hand-rolled force-directed graph. The point is traversal: start from a
 * principal or an item, see the identity/group/workspace/item chain, and
 * click any node to re-root the graph there. */

const graphState = { nodes: [], links: [], sim: null, hover: null, pan: { x: 0, y: 0 }, zoom: 1, root: null };

PANES.graph = async function (root) {
  root.innerHTML = "";

  const controls = el("div", "search-row");
  const modeWrap = el("div", "pill-row");
  modeWrap.style.margin = "0";
  ["Principal focus", "Item focus", "Workspace focus"].forEach((m, i) => {
    const p = el("div", `pill${(graphState.mode || "Principal focus") === m ? " on" : ""}`, m);
    p.onclick = () => { graphState.mode = m; PANES.graph(root); };
    modeWrap.appendChild(p);
  });
  controls.appendChild(modeWrap);
  const reset = el("button", "ghost", "Reset view");
  reset.onclick = () => { graphState.pan = { x: 0, y: 0 }; graphState.zoom = 1; };
  controls.appendChild(reset);
  root.appendChild(controls);

  const mode = graphState.mode || "Principal focus";

  const holder = el("div");
  holder.style.position = "relative";
  const c = card("Access graph",
    "Identity → group → workspace → item. Click any node to re-pivot the whole app; drag to pan, scroll to zoom.",
    holder);
  const canvas = el("canvas");
  canvas.id = "graphCanvas";
  holder.appendChild(canvas);
  const tip = el("div", "graph-tip");
  holder.appendChild(tip);
  root.appendChild(c);

  const legend = el("div", "legend");
  legend.innerHTML = [
    [CHART_COLORS[0], "User"], [CHART_COLORS[1], "Group"], [CHART_COLORS[2], "Service principal"],
    [CHART_COLORS[3], "Workspace"], [CHART_COLORS[5], "Item"],
  ].map(([col, l]) => `<span><i style="background:${col}"></i>${l}</span>`).join("");
  root.appendChild(legend);

  const detail = el("div");
  detail.style.marginTop = "14px";
  root.appendChild(detail);

  const ctx = canvas.getContext("2d");
  const status = el("div", "loading", '<span class="spinner"></span>Building graph…');
  holder.appendChild(status);

  let rows;
  try {
    rows = await runDax(
      scoped(
        ["'dim_principal'[display_name]", "'dim_principal'[principal_type]",
         "'dim_item'[workspace_name]", "'dim_item'[item_name]", "'dim_item'[item_type]",
         "'fact_effective_access'[granted_via_name]", "'fact_effective_access'[is_via_group]",
         "'fact_effective_access'[permission_name]", "'fact_effective_access'[grant_source]"],
        ['"paths", [Access Paths]']
      )
    );
  } catch (e) {
    status.remove();
    holder.appendChild(el("div", "err", "Graph query failed: " + escapeHtml(e.message)));
    return;
  }
  status.remove();

  if (!rows.length) {
    holder.appendChild(el("div", "empty", "No access paths under the current filters."));
    return;
  }

  /* --- build node/link sets. Cap the graph so it stays legible; the tables
     below always show the full picture. */
  const CAP = 260;
  const nodes = new Map();
  const links = [];
  const addNode = (id, label, type, meta) => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        id, label, type, meta,
        x: (Math.random() - 0.5) * 600, y: (Math.random() - 0.5) * 400,
        vx: 0, vy: 0, deg: 0,
      });
    }
    return nodes.get(id);
  };
  const addLink = (a, b, label) => {
    links.push({ s: a.id, t: b.id, label });
    a.deg++; b.deg++;
  };

  const NODE_COLORS = {
    User: CHART_COLORS[0], Group: CHART_COLORS[1], ServicePrincipal: CHART_COLORS[2],
    ManagedIdentity: CHART_COLORS[2], Workspace: CHART_COLORS[3],
    Item: CHART_COLORS[5], Unknown: THEME.dim(),
  };

  const slice = rows.slice(0, 1200);
  slice.forEach((r) => {
    const pn = pick(r, "[display_name]") || "(unknown)";
    const pt = pick(r, "[principal_type]") || "Unknown";
    const ws = pick(r, "[workspace_name]") || "(no workspace)";
    const it = pick(r, "[item_name]") || "(no item)";
    const itype = pick(r, "[item_type]");
    const via = pick(r, "[granted_via_name]");
    const viaGrp = pick(r, "[is_via_group]");
    const perm = pick(r, "[permission_name]");

    const pNode = addNode("p|" + pn, pn, pt, { kind: "principal", type: pt });
    const wNode = addNode("w|" + ws, ws, "Workspace", { kind: "workspace" });
    const iNode = addNode("i|" + ws + "|" + it, it, "Item", { kind: "item", itemType: itype, ws });

    if (mode !== "Workspace focus" && viaGrp && via) {
      const gNode = addNode("g|" + via, via, "Group", { kind: "principal", type: "Group" });
      addLink(pNode, gNode, "member of");
      addLink(gNode, wNode, perm);
    } else {
      addLink(pNode, wNode, perm);
    }
    addLink(wNode, iNode, itype);
  });

  // Keep the densest neighbourhood when the graph is large.
  let nodeList = [...nodes.values()];
  if (nodeList.length > CAP) {
    nodeList.sort((a, b) => b.deg - a.deg);
    const keep = new Set(nodeList.slice(0, CAP).map((n) => n.id));
    nodeList = nodeList.filter((n) => keep.has(n.id));
    graphState.truncated = nodes.size - nodeList.length;
  } else {
    graphState.truncated = 0;
  }
  const keepIds = new Set(nodeList.map((n) => n.id));
  const linkList = links.filter((l) => keepIds.has(l.s) && keepIds.has(l.t));
  const byId = new Map(nodeList.map((n) => [n.id, n]));

  graphState.nodes = nodeList;
  graphState.links = linkList;

  /* --- layout: simple spring/repulsion iterated on rAF */
  let W = 0, H = 0;
  function resize() {
    const dpr = window.devicePixelRatio || 1;
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  resize();
  window.addEventListener("resize", resize);

  const nodeRadius = (n) => Math.min(20, 5 + Math.sqrt(n.deg) * 2.2);

  // Simulated-annealing cooling factor. Named "heat" rather than "alpha"
  // deliberately: a global withAlpha() colour helper exists, and a local `alpha`
  // shadowed it, so the first draw() threw and the canvas stayed blank.
  let heat = 1;
  function step() {
    const k = 0.9;
    // repulsion
    for (let i = 0; i < nodeList.length; i++) {
      const a = nodeList[i];
      for (let j = i + 1; j < nodeList.length; j++) {
        const b = nodeList[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { d2 = 1; dx = Math.random(); dy = Math.random(); }
        if (d2 > 90000) continue;
        const f = (1400 * heat) / d2;
        const d = Math.sqrt(d2);
        a.vx -= (dx / d) * f; a.vy -= (dy / d) * f;
        b.vx += (dx / d) * f; b.vy += (dy / d) * f;
      }
    }
    // springs
    linkList.forEach((l) => {
      const a = byId.get(l.s), b = byId.get(l.t);
      if (!a || !b) return;
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const f = ((d - 90) * 0.012) * heat;
      a.vx += (dx / d) * f; a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
    });
    // centering + integrate
    nodeList.forEach((n) => {
      n.vx -= n.x * 0.0022 * heat;
      n.vy -= n.y * 0.0022 * heat;
      n.x += (n.vx *= k); n.y += (n.vy *= k);
    });
    heat = Math.max(0.02, heat * 0.994);
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    ctx.save();
    ctx.translate(W / 2 + graphState.pan.x, H / 2 + graphState.pan.y);
    ctx.scale(graphState.zoom, graphState.zoom);

    const hoverId = graphState.hover?.id;
    const connected = new Set();
    if (hoverId) {
      linkList.forEach((l) => {
        if (l.s === hoverId) connected.add(l.t);
        if (l.t === hoverId) connected.add(l.s);
      });
    }

    linkList.forEach((l) => {
      const a = byId.get(l.s), b = byId.get(l.t);
      if (!a || !b) return;
      const lit = hoverId && (l.s === hoverId || l.t === hoverId);
      ctx.strokeStyle = lit ? withAlpha(THEME.accent(), .85) : withAlpha(THEME.border2(), .5);
      ctx.lineWidth = lit ? 1.7 : 0.7;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
      ctx.stroke();
    });

    nodeList.forEach((n) => {
      const r = nodeRadius(n);
      const dim = hoverId && n.id !== hoverId && !connected.has(n.id);
      ctx.globalAlpha = dim ? 0.28 : 1;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fillStyle = NODE_COLORS[n.type] || NODE_COLORS.Unknown;
      ctx.fill();
      if (n.id === hoverId) {
        ctx.lineWidth = 2.4; ctx.strokeStyle = "#fff"; ctx.stroke();
      }
      if (graphState.zoom > 0.75 && (r > 8 || n.id === hoverId || connected.has(n.id))) {
        ctx.fillStyle = withAlpha(cssVar("--text", "#e8f6f2"), .9);
        ctx.font = "11px 'Segoe UI', sans-serif";
        ctx.textAlign = "center";
        const lbl = n.label.length > 22 ? n.label.slice(0, 21) + "…" : n.label;
        ctx.fillText(lbl, n.x, n.y + r + 12);
      }
      ctx.globalAlpha = 1;
    });
    ctx.restore();
  }

  let running = true;
  (function loop() {
    if (!running || !document.body.contains(canvas)) { running = false; return; }
    try {
      step(); draw();
    } catch (e) {
      // A throw inside rAF kills the loop and leaves an empty canvas with no
      // clue why. Surface it instead of failing silently.
      running = false;
      status.remove();
      holder.appendChild(el("div", "err", "Graph render failed: " + escapeHtml(e.message)));
      console.error("[onesafe] graph render failed", e);
      return;
    }
    requestAnimationFrame(loop);
  })();

  /* --- interaction */
  const toWorld = (ev) => {
    const rect = canvas.getBoundingClientRect();
    return {
      x: (ev.clientX - rect.left - W / 2 - graphState.pan.x) / graphState.zoom,
      y: (ev.clientY - rect.top - H / 2 - graphState.pan.y) / graphState.zoom,
    };
  };
  const hit = (pt) =>
    nodeList.find((n) => {
      const r = nodeRadius(n) + 4;
      return (n.x - pt.x) ** 2 + (n.y - pt.y) ** 2 <= r * r;
    });

  let dragging = false, last = null, moved = 0;
  canvas.onmousedown = (e) => { dragging = true; last = { x: e.clientX, y: e.clientY }; moved = 0; };
  window.addEventListener("mouseup", () => (dragging = false));
  canvas.onmousemove = (e) => {
    if (dragging && last) {
      graphState.pan.x += e.clientX - last.x;
      graphState.pan.y += e.clientY - last.y;
      moved += Math.abs(e.clientX - last.x) + Math.abs(e.clientY - last.y);
      last = { x: e.clientX, y: e.clientY };
      tip.style.display = "none";
      return;
    }
    const n = hit(toWorld(e));
    graphState.hover = n || null;
    canvas.style.cursor = n ? "pointer" : "grab";
    if (n) {
      const rect = canvas.getBoundingClientRect();
      tip.style.display = "block";
      tip.style.left = e.clientX - rect.left + 14 + "px";
      tip.style.top = e.clientY - rect.top + 14 + "px";
      tip.innerHTML =
        `<b>${escapeHtml(n.label)}</b><br>` +
        `<span style="color:var(--muted)">${escapeHtml(n.type)} · ${n.deg} connection${n.deg === 1 ? "" : "s"}</span>` +
        `<br><span style="color:var(--dim);font-size:11px">click to focus</span>`;
    } else {
      tip.style.display = "none";
    }
  };
  canvas.onmouseleave = () => { tip.style.display = "none"; graphState.hover = null; };
  canvas.onwheel = (e) => {
    e.preventDefault();
    graphState.zoom = Math.min(3, Math.max(0.3, graphState.zoom * (e.deltaY < 0 ? 1.12 : 0.89)));
  };
  canvas.onclick = (e) => {
    if (moved > 5) return;
    const n = hit(toWorld(e));
    if (!n) return;
    running = false;
    if (n.meta.kind === "principal") setFilter("principal", n.label);
    else if (n.meta.kind === "workspace") setFilter("workspace", n.label);
    else setFilter("item", n.label);
  };

  if (graphState.truncated) {
    const note = el("div", "sub");
    note.style.cssText = "margin-top:8px;color:var(--amber);font-size:11.5px";
    note.textContent =
      `Showing the ${CAP} most connected nodes of ${nodes.size}. Narrow the filters to see the rest — the tables below always reflect everything.`;
    root.appendChild(note);
  }

  /* --- companion table so nothing is hidden by the layout cap */
  fill(detail, async () => {
    const agg = new Map();
    slice.forEach((r) => {
      const via = pick(r, "[is_via_group]") ? pick(r, "[granted_via_name]") : "(direct)";
      const key = via + "|" + pick(r, "[grant_source]");
      const cur = agg.get(key) || { via, src: pick(r, "[grant_source]"), paths: 0, ppl: new Set(), items: new Set() };
      cur.paths += Number(pick(r, "[paths]")) || 0;
      cur.ppl.add(pick(r, "[display_name]"));
      cur.items.add(pick(r, "[item_name]"));
      agg.set(key, cur);
    });
    const rows2 = [...agg.values()].map((v) => ({
      "[via]": v.via, "[src]": v.src, "[paths]": v.paths,
      "[ppl]": v.ppl.size, "[items]": v.items.size,
    }));
    return card("Grant channels", "Each group or direct grant, and how much reach it confers",
      dataTable(rows2, [
        { key: "[via]", label: "Granted via", render: (v) => v === "(direct)" ? '<span class="badge b-neutral">direct</span>' : `<span class="badge b-group">${escapeHtml(v)}</span>` },
        { key: "[src]", label: "Source" },
        { key: "[ppl]", label: "Principals", num: true, render: nf },
        { key: "[items]", label: "Items", num: true, render: nf },
        { key: "[paths]", label: "Paths", num: true, render: nf },
      ], { maxHeight: "320px", sortKey: "[paths]" }));
  }, "Grant channels");
};

/* ===================================================================== */
/* 5. ONELAKE SECURITY                                                    */
/* ===================================================================== */

PANES.onelake = async function (root) {
  root.innerHTML = "";

  const kpis = el("div", "grid g5");
  root.appendChild(kpis);
  fill(kpis, async () => {
    const r = await runDax(
      scalars([
        '"roles", [OneLake Roles]',
        '"custom", [Custom OneLake Roles]',
        '"members", [OneLake Role Members]',
        '"rules", [OneLake Rules]',
        '"secured", [Items with OneLake Security]',
        '"scanned", [OneLake Items Scanned]',
        '"gaps", [OneLake Scan Gaps]',
        '"disabled", [OneLake Feature Disabled Items]',
        '"cov", [OneLake Coverage %]',
      ])
    );
    const g = el("div", "grid g5");
    g.appendChild(kpi("Items with OneLake security", nf(val(r, "[secured]")), `of ${nf(val(r, "[scanned]"))} scanned`));
    g.appendChild(kpi("Data access roles", nf(val(r, "[roles]")), `${nf(val(r, "[custom]"))} custom`));
    g.appendChild(kpi("Role members", nf(val(r, "[members]")), "identities scoped at the data plane"));
    g.appendChild(kpi("Scoping rules", nf(val(r, "[rules]")), "path and permission rules"));
    // An item with the feature switched off cannot be scoped at all, which is a
    // different finding from an item that simply has no roles yet.
    g.appendChild(kpi("Security not enabled", nf(val(r, "[disabled]")),
      "items where OneLake security is switched off",
      val(r, "[disabled]") ? "warn" : "good"));
    g.appendChild(kpi("Scan coverage", pctFmt(val(r, "[cov]")),
      `${nf(val(r, "[gaps]"))} item(s) unreadable by the scanner`,
      val(r, "[gaps]") ? "warn" : "good",
      () => setFilter("restricted", state.filters.restricted ? null : true)));
    return g;
  }, "OneLake KPIs");

  root.appendChild(section("Coverage",
    "OneSafe reports what it could and could not read. Gaps are shown explicitly rather than assumed safe."));
  const cov = el("div", "grid g-2-1");
  root.appendChild(cov);
  const cA = el("div"), cB = el("div");
  cov.append(cA, cB);

  fill(cA, async () => {
    const rows = await runDax(
      `EVALUATE
CALCULATETABLE(
  SUMMARIZECOLUMNS(
    'dim_item'[workspace_name],
    'dim_item'[item_name],
    'dim_item'[item_type],
    'fact_onelake_coverage'[access_denied],
    'fact_onelake_coverage'[coverage_status],
    'fact_onelake_coverage'[role_count],
    'fact_onelake_coverage'[error],
    "n", [OneLake Items Scanned]
  ),
  ${[snapshotExpr(), ...filterExprs()].join(",\n  ")}
)`
    );
    return card(`OneLake-capable items (${nf(rows.length)})`,
      "Lakehouses, warehouses and mirrored databases the scanner enumerated",
      dataTable(rows, [
        { key: "[workspace_name]", label: "Workspace" },
        { key: "[item_name]", label: "Item" },
        { key: "[item_type]", label: "Type" },
        { key: "[role_count]", label: "Roles", num: true, render: (v) => Number(v) ? `<b>${nf(v)}</b>` : '<span class="dim">0</span>' },
        { key: "[coverage_status]", label: "Scan result", render: coverageBadge },
        { key: "[error]", label: "Detail", render: (v) => v ? `<span class="trunc" title="${escapeHtml(v)}">${escapeHtml(v)}</span>` : "—" },
      ], {
        maxHeight: "430px", sortKey: "[role_count]",
        onRowClick: (r) => { setFilter("item", pick(r, "[item_name]")); showPane("item"); },
      }));
  }, "Coverage");

  fill(cB, async () => {
    const rows = await runDax(
      scoped(["'fact_effective_access'[data_plane_restricted]"], ['"paths", [Access Paths]'], ["restricted"])
    );
    const body = el("div");
    const box = el("div", "chart-box");
    box.style.setProperty("--ch", "200px");
    box.innerHTML = '<canvas id="olChart"></canvas>';
    body.appendChild(box);
    const note = el("div", "sub");
    note.style.marginTop = "10px";
    note.textContent =
      "A restricted path means the identity can open the item, but OneLake data access roles narrow which folders, tables, rows or columns are actually readable.";
    body.appendChild(note);
    const c = card("Data-plane constraint", "Access paths limited by OneLake security", body);
    setTimeout(() => {
      const labels = rows.map((r) => (pick(r, "[data_plane_restricted]") ? "Restricted" : "Full data access"));
      drawChart("olChart", {
        type: "doughnut",
        data: {
          labels,
          datasets: [{
            data: rows.map((r) => Number(pick(r, "[paths]")) || 0),
            backgroundColor: [CHART_COLORS[2], CHART_COLORS[0]],
            borderColor: THEME.panel(), borderWidth: 2,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false, cutout: "62%",
          plugins: { legend: { position: "bottom", labels: { boxWidth: 11, font: { size: 11 } } } },
        },
      });
    }, 0);
    return c;
  }, "Restriction split");

  root.appendChild(section("Roles, rules and members"));
  const g2 = el("div", "grid g2");
  root.appendChild(g2);
  const r1 = el("div"), r2 = el("div");
  g2.append(r1, r2);

  fill(r1, async () => {
    const rows = await runDax(
      `EVALUATE
SELECTCOLUMNS(
  CALCULATETABLE('fact_onelake_rule', ${snapshotExpr()}),
  "role_name", LOOKUPVALUE('fact_onelake_role'[role_name],
      'fact_onelake_role'[role_id], 'fact_onelake_rule'[role_id],
      'fact_onelake_role'[snapshot_date], 'fact_onelake_rule'[snapshot_date]),
  "item_name", LOOKUPVALUE('dim_item'[item_name],
      'dim_item'[item_id], 'fact_onelake_rule'[item_id]),
  "effect", 'fact_onelake_rule'[effect],
  "path", 'fact_onelake_rule'[path],
  "permissions", 'fact_onelake_rule'[permissions]
)`
    );
    return card(`Access rules (${nf(rows.length)})`, "Path scoping and granted permissions per role",
      dataTable(rows, [
        { key: "[item_name]", label: "Item" },
        { key: "[role_name]", label: "Role" },
        { key: "[effect]", label: "Effect", render: (v) => `<span class="badge ${String(v).toLowerCase() === "permit" ? "b-ok" : "b-risk"}">${escapeHtml(v)}</span>` },
        { key: "[path]", label: "Path" },
        { key: "[permissions]", label: "Permissions" },
      ], { maxHeight: "380px", emptyText: "No OneLake security rules defined in this tenant yet." }));
  }, "Rules");

  fill(r2, async () => {
    const rows = await runDax(
      `EVALUATE
SELECTCOLUMNS(
  CALCULATETABLE('fact_onelake_role_member', ${snapshotExpr()}),
  "principal_name", LOOKUPVALUE('dim_principal'[display_name],
      'dim_principal'[principal_id], 'fact_onelake_role_member'[principal_id]),
  "principal_type", 'fact_onelake_role_member'[principal_type],
  "role_name", LOOKUPVALUE('fact_onelake_role'[role_name],
      'fact_onelake_role'[role_id], 'fact_onelake_role_member'[role_id],
      'fact_onelake_role'[snapshot_date], 'fact_onelake_role_member'[snapshot_date]),
  "item_name", LOOKUPVALUE('dim_item'[item_name],
      'dim_item'[item_id], 'fact_onelake_role_member'[item_id]),
  "source_type", 'fact_onelake_role_member'[source_type]
)`
    );
    return card(`Role members (${nf(rows.length)})`, "Click a member to open their full 360 view",
      dataTable(rows, [
        { key: "[principal_name]", label: "Principal" },
        { key: "[principal_type]", label: "Type", render: (v) => `<span class="badge b-neutral">${escapeHtml(v)}</span>` },
        { key: "[item_name]", label: "Item" },
        { key: "[role_name]", label: "Role" },
        { key: "[source_type]", label: "Source" },
      ], {
        maxHeight: "380px",
        emptyText: "No OneLake role members.",
        onRowClick: (r) => {
          const n = pick(r, "[principal_name]");
          if (n) { setFilter("principal", n); showPane("principal"); }
        },
      }));
  }, "Members");
};

/* ===================================================================== */
/* 5b. DATA SECURITY - ROW AND COLUMN LEVEL                               */
/* ===================================================================== */

// Workspace and item permissions answer "can they open it". Row- and
// column-level rules answer "what do they actually see once they do", and the
// two are set in completely different places: RLS/CLS roles inside a semantic
// model (TMSL) and row/column constraints inside OneLake data access roles.
// This pane is the only place both planes are shown side by side.
PANES.datasec = async function (root) {
  root.innerHTML = "";

  const kpis = el("div", "grid g5");
  root.appendChild(kpis);
  fill(kpis, async () => {
    const r = await runDax(
      scalars([
        '"rules", [Data Security Rules]',
        '"rls", [RLS Rules]',
        '"cls", [CLS Rules]',
        '"model", [Model RLS/CLS Rules]',
        '"onelake", [OneLake RLS/CLS Rules]',
        '"roles", [Data Security Roles]',
        '"items", [Items with RLS or CLS]',
        '"principals", [Principals under RLS or CLS]',
        '"dynamic", [Dynamic RLS Rules]',
        '"unassigned", [Unassigned Data Security Rules]',
        '"cov", [Model Security Coverage %]',
        '"gaps", [Model Security Read Gaps]',
      ])
    );
    const g = el("div", "grid g5");
    g.appendChild(kpi("Row-level rules", nf(val(r, "[rls]")),
      "filters that hide rows from a principal"));
    g.appendChild(kpi("Column-level rules", nf(val(r, "[cls]")),
      "columns hidden from a principal"));
    g.appendChild(kpi("Items restricted", nf(val(r, "[items]")),
      `across ${nf(val(r, "[roles]"))} security role(s)`));
    g.appendChild(kpi("Principals restricted", nf(val(r, "[principals]")),
      "identities whose view is narrowed"));
    // A rule nobody is assigned to restricts nobody. It looks like security in
    // the portal and is worth surfacing loudly.
    g.appendChild(kpi("Rules with no member", nf(val(r, "[unassigned]")),
      "defined but assigned to nobody",
      val(r, "[unassigned]") ? "warn" : "good"));
    g.appendChild(kpi("Semantic model plane", nf(val(r, "[model]")),
      "RLS/CLS defined inside a model"));
    g.appendChild(kpi("OneLake plane", nf(val(r, "[onelake]")),
      "row/column constraints on lakehouse data"));
    g.appendChild(kpi("Dynamic filters", nf(val(r, "[dynamic]")),
      "resolve per signed-in user at query time"));
    g.appendChild(kpi("Model read coverage", pctFmt(val(r, "[cov]")),
      `${nf(val(r, "[gaps]"))} model(s) unreadable`,
      val(r, "[gaps]") ? "warn" : "good"));
    g.appendChild(kpi("Total rules", nf(val(r, "[rules]")),
      "row and column rules, per assigned principal"));
    return g;
  }, "Data security KPIs");

  root.appendChild(section("Where row and column security is applied",
    "Both planes are shown together. An item can be restricted in one, the other, or both."));

  const top = el("div", "grid g-2-1");
  root.appendChild(top);
  const tA = el("div"), tB = el("div");
  top.append(tA, tB);

  fill(tA, async () => {
    const rows = await runDax(
      `EVALUATE
CALCULATETABLE(
  SUMMARIZECOLUMNS(
    'dim_item'[workspace_name],
    'dim_item'[item_name],
    'dim_item'[item_type],
    'fact_data_security'[plane],
    "roles", [Data Security Roles],
    "rls", [RLS Rules],
    "cls", [CLS Rules],
    "principals", [Principals under RLS or CLS],
    "dyn", [Dynamic RLS Rules]
  ),
  ${[snapshotExpr(), ...filterExprs()].join(",\n  ")}
)`
    );
    return card(`Restricted items (${nf(rows.length)})`,
      "Click an item to open its full access view",
      dataTable(rows, [
        { key: "[workspace_name]", label: "Workspace" },
        { key: "[item_name]", label: "Item" },
        { key: "[plane]", label: "Plane", render: planeBadge },
        { key: "[rls]", label: "RLS", num: true, render: (v) => Number(v) ? `<span class="badge b-build">${nf(v)} row</span>` : '<span class="dim">—</span>' },
        { key: "[cls]", label: "CLS", num: true, render: (v) => Number(v) ? `<span class="badge b-reshare">${nf(v)} col</span>` : '<span class="dim">—</span>' },
        { key: "[dyn]", label: "Dynamic", num: true, render: (v) => Number(v) ? '<span class="badge b-warn">dynamic</span>' : '<span class="dim">static</span>' },
        { key: "[principals]", label: "Principals", num: true },
      ], {
        maxHeight: "420px", sortKey: "[rls]",
        emptyText: "No row- or column-level security found in this tenant.",
        onRowClick: (r) => { setFilter("item", pick(r, "[item_name]")); showPane("item"); },
      }));
  }, "Restricted items");

  fill(tB, async () => {
    const rows = await runDax(
      scoped(["'fact_data_security'[plane]", "'fact_data_security'[rule_type]"],
        ['"rules", [Data Security Rules]'])
    );
    const body = el("div");
    const box = el("div", "chart-box");
    box.style.setProperty("--ch", "210px");
    box.innerHTML = '<canvas id="dsChart"></canvas>';
    body.appendChild(box);
    const note = el("div", "sub");
    note.style.marginTop = "10px";
    note.textContent =
      "Row-level security filters which records are returned. Column-level security removes columns entirely. Both are invisible in a permissions list, which is why they are tracked separately here.";
    body.appendChild(note);
    const c = card("Rules by plane and type", "Where the restriction is enforced", body);
    setTimeout(() => {
      const planes = [...new Set(rows.map((r) => pick(r, "[plane]")).filter(Boolean))];
      const types = [...new Set(rows.map((r) => pick(r, "[rule_type]")).filter(Boolean))];
      drawChart("dsChart", {
        type: "bar",
        data: {
          labels: planes,
          datasets: types.map((t, i) => ({
            label: t,
            data: planes.map((p) => {
              const hit = rows.find((r) => pick(r, "[plane]") === p && pick(r, "[rule_type]") === t);
              return hit ? Number(pick(hit, "[rules]")) || 0 : 0;
            }),
            backgroundColor: CHART_COLORS[i % CHART_COLORS.length],
            borderRadius: 6,
          })),
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true } },
          plugins: { legend: { position: "bottom", labels: { boxWidth: 11, font: { size: 11 } } } },
        },
      });
    }, 0);
    return c;
  }, "Rule split");

  root.appendChild(section("Every rule, in full",
    "The verbatim filter expression or predicate, and who it applies to."));

  fill(root.appendChild(el("div")), async () => {
    const rows = await runDax(
      `EVALUATE
CALCULATETABLE(
  SELECTCOLUMNS(
    'fact_data_security',
    "item_name", LOOKUPVALUE('dim_item'[item_name],
        'dim_item'[item_id], 'fact_data_security'[item_id]),
    "workspace_name", LOOKUPVALUE('dim_item'[workspace_name],
        'dim_item'[item_id], 'fact_data_security'[item_id]),
    "plane", 'fact_data_security'[plane],
    "role_name", 'fact_data_security'[role_name],
    "rule_type", 'fact_data_security'[rule_type],
    "scope_table", 'fact_data_security'[scope_table],
    "scope_column", 'fact_data_security'[scope_column],
    "rule_summary", 'fact_data_security'[rule_summary],
    "rule_expression", 'fact_data_security'[rule_expression],
    "is_dynamic", 'fact_data_security'[is_dynamic],
    "has_member", 'fact_data_security'[has_member],
    "principal_name", LOOKUPVALUE('dim_principal'[display_name],
        'dim_principal'[principal_id], 'fact_data_security'[principal_id]),
    "principal_upn", 'fact_data_security'[principal_upn]
  ),
  ${[snapshotExpr(), ...filterExprs()].join(",\n  ")}
)`
    );
    return card(`Rule detail (${nf(rows.length)})`,
      "One row per rule and assigned principal. Click to pivot to that principal.",
      dataTable(rows, [
        { key: "[workspace_name]", label: "Workspace" },
        { key: "[item_name]", label: "Item" },
        { key: "[plane]", label: "Plane", render: planeBadge },
        { key: "[role_name]", label: "Role" },
        {
          key: "[rule_type]", label: "Type",
          render: (v, r) => {
            const t = String(v || "");
            const base = t === "CLS"
              ? '<span class="badge b-reshare">column</span>'
              : '<span class="badge b-build">row</span>';
            return pick(r, "[is_dynamic]")
              ? base + ' <span class="badge b-warn">dynamic</span>' : base;
          },
        },
        { key: "[scope_table]", label: "Scope", render: (v, r) => renderScope(v, pick(r, "[scope_column]")) },
        { key: "[rule_summary]", label: "Rule", render: (v, r) => renderRule(v, pick(r, "[rule_expression]")) },
        {
          key: "[principal_name]", label: "Applies to",
          render: (v, r) => {
            if (v) {
              const upn = pick(r, "[principal_upn]");
              return `<span title="${escapeHtml(upn || "")}">${escapeHtml(v)}</span>`;
            }
            return pick(r, "[has_member]")
              ? '<span class="dim">unresolved</span>'
              : '<span class="badge b-warn">no members</span>';
          },
        },
      ], {
        maxHeight: "520px",
        emptyText: "No row- or column-level rules found.",
        onRowClick: (r) => {
          const n = pick(r, "[principal_name]");
          if (n) { setFilter("principal", n); showPane("principal"); }
        },
      }));
  }, "Rule detail");
};

/* ===================================================================== */
/* 6. RISK & DRIFT                                                        */
/* ===================================================================== */

const RISK_CATALOG = [
  ["ItemResharePrivilege", "Item reshare privilege", "Holders can re-grant this item to anyone else, spreading access outside any review."],
  ["OrphanedPrincipal", "Orphaned principal", "The identity is disabled or no longer resolves in Entra, yet the grant survives."],
  ["ServicePrincipalWriteAccess", "Service principal write", "A non-human identity can modify content. Compromise means silent change."],
  ["GuestAccess", "Guest access", "An external identity holds access to tenant content."],
  ["BroadGroupGrant", "Broad group grant", "Access flows through a very large group, so the effective audience is far wider than it looks."],
  ["GroupGrantOnSecuredData", "Group grant on secured data", "A group grant lands on an item that also carries OneLake security, making effective access hard to reason about."],
];

PANES.risk = async function (root) {
  root.innerHTML = "";

  const kpis = el("div");
  root.appendChild(kpis);
  fill(kpis, async () => {
    const r = await runDax(
      scalars([
        '"risk", [Risk Paths]',
        '"pct", [Risk Path %]',
        '"ppl", [Risky Principals]',
        '"added", [Access Added]',
        '"removed", [Access Removed]',
        '"elev", [Access Elevated]',
      ])
    );
    const g = el("div", "grid g5");
    g.appendChild(kpi("Risky paths", nf(val(r, "[risk]")), pctFmt(val(r, "[pct]")) + " of all access", "risk"));
    g.appendChild(kpi("Risky principals", nf(val(r, "[ppl]")), "identities holding at least one flagged path", "risk"));
    g.appendChild(kpi("Access added", nf(val(r, "[added]")), "since the previous snapshot", "warn"));
    g.appendChild(kpi("Access removed", nf(val(r, "[removed]")), "since the previous snapshot"));
    g.appendChild(kpi("Access elevated", nf(val(r, "[elev]")), "permission level increased", "risk"));
    return g;
  }, "Risk KPIs");

  root.appendChild(section("Risk catalogue", "Click a card to filter the entire app to that finding."));
  const cat = el("div", "grid g3");
  root.appendChild(cat);
  fill(cat, async () => {
    const r = await runDax(
      scalars([
        '"ItemResharePrivilege", [Item Reshare Paths]',
        '"OrphanedPrincipal", [Orphaned Access Paths]',
        '"ServicePrincipalWriteAccess", [Service Principal Write Paths]',
        '"GuestAccess", [Guest Access Paths]',
        '"BroadGroupGrant", [Broad Group Paths]',
        '"GroupGrantOnSecuredData", [Group Grants on Secured Data]',
      ]),
    );
    const g = el("div", "grid g3");
    RISK_CATALOG.forEach(([flag, label, why]) => {
      const n = val(r, `[${flag}]`);
      const c = el("div", `card kpi ${n ? "risk" : "good"} clickable`);
      c.innerHTML =
        `<div class="label">${escapeHtml(label)}</div>` +
        `<div class="value">${nf(n)}</div>` +
        `<div class="foot" style="line-height:1.45">${escapeHtml(why)}</div>`;
      c.onclick = () => setFilter("risk", state.filters.risk === flag ? null : flag);
      if (state.filters.risk === flag) c.style.borderColor = "var(--accent)";
      g.appendChild(c);
    });
    return g;
  }, "Risk catalogue");

  root.appendChild(section("Riskiest identities"));
  const two = el("div", "grid g2");
  root.appendChild(two);
  const t1 = el("div"), t2 = el("div");
  two.append(t1, t2);

  fill(t1, async () => {
    const rows = await runDax(
      scoped(["'dim_principal'[display_name]", "'dim_principal'[principal_type]", "'dim_principal'[is_orphaned]"],
        ['"risk", [Risk Paths]', '"paths", [Access Paths]', '"items", [Items Accessible]'],
        ["principal", "principalId"])
    );
    const risky = rows.filter((r) => Number(pick(r, "[risk]")) > 0);
    return card(`Principals carrying risk (${nf(risky.length)})`, "Click to open their 360 view",
      dataTable(risky, [
        { key: "[display_name]", label: "Principal" },
        { key: "[principal_type]", label: "Type", render: (v) => `<span class="badge b-neutral">${escapeHtml(v)}</span>` },
        { key: "[is_orphaned]", label: "State", render: (v) => v ? '<span class="badge b-risk">orphaned</span>' : '<span class="badge b-ok">active</span>' },
        { key: "[items]", label: "Items", num: true, render: nf },
        { key: "[risk]", label: "Risky paths", num: true, render: (v) => `<b style="color:var(--red)">${nf(v)}</b>` },
        { key: "[paths]", label: "Total paths", num: true, render: nf },
      ], {
        maxHeight: "420px", sortKey: "[risk]",
        onRowClick: (r) => { setFilter("principal", pick(r, "[display_name]")); showPane("principal"); },
      }));
  }, "Risky principals");

  fill(t2, async () => {
    const rows = await runDax(
      scoped(["'dim_item'[workspace_name]", "'dim_item'[item_name]", "'dim_item'[item_type]"],
        ['"risk", [Risk Paths]', '"ppl", [Principals with Access]'], ["item", "itemId"])
    );
    const risky = rows.filter((r) => Number(pick(r, "[risk]")) > 0);
    return card(`Items carrying risk (${nf(risky.length)})`, "Click to open the item view",
      dataTable(risky, [
        { key: "[workspace_name]", label: "Workspace" },
        { key: "[item_name]", label: "Item" },
        { key: "[item_type]", label: "Type" },
        { key: "[ppl]", label: "Principals", num: true, render: nf },
        { key: "[risk]", label: "Risky paths", num: true, render: (v) => `<b style="color:var(--red)">${nf(v)}</b>` },
      ], {
        maxHeight: "420px", sortKey: "[risk]",
        onRowClick: (r) => { setFilter("item", pick(r, "[item_name]")); showPane("item"); },
      }));
  }, "Risky items");

  root.appendChild(section("Drift", "What changed between the two most recent snapshots"));
  const drift = el("div");
  root.appendChild(drift);
  fill(drift, async () => {
    const rows = await runDax(
      `EVALUATE
CALCULATETABLE(
  SUMMARIZECOLUMNS(
    'fact_access_change'[change_type],
    'dim_principal'[display_name],
    'dim_item'[item_name],
    'dim_item'[workspace_name],
    'fact_access_change'[prev_permission_name],
    'fact_access_change'[new_permission_name],
    'fact_access_change'[prev_snapshot_date],
    'fact_access_change'[access_path],
    "n", [Access Changes]
  ),
  ${[snapshotExpr(), ...filterExprs(["permission", "source", "risk", "viaGroup", "restricted"])].join(",\n  ")}
)`
    );
    if (!rows.length) {
      return card("Change feed", "",
        el("div", "empty",
          "No changes recorded yet. The drift feed populates once OneSafe has captured two daily snapshots."));
    }
    return card(`Change feed (${nf(rows.length)})`, "Added, removed and elevated grants",
      dataTable(rows, [
        { key: "[change_type]", label: "Change", render: (v) => {
            const cls = v === "Added" ? "b-write" : v === "Removed" ? "b-neutral" : "b-risk";
            return `<span class="badge ${cls}">${escapeHtml(v)}</span>`;
          } },
        { key: "[display_name]", label: "Principal" },
        { key: "[workspace_name]", label: "Workspace" },
        { key: "[item_name]", label: "Item" },
        { key: "[prev_permission_name]", label: "Was", render: (v) => v ? permBadge(v) : "—" },
        { key: "[new_permission_name]", label: "Now", render: (v) => v ? permBadge(v) : "—" },
        { key: "[access_path]", label: "Access path", render: renderPath },
      ], {
        maxHeight: "460px",
        onRowClick: (r) => { setFilter("principal", pick(r, "[display_name]")); showPane("principal"); },
      }));
  }, "Change feed");

  root.appendChild(section("Trend", "Access surface over every captured snapshot"));
  const trend = el("div");
  root.appendChild(trend);
  fill(trend, async () => {
    const rows = await runDax(
      `EVALUATE
CALCULATETABLE(
  SUMMARIZECOLUMNS(
    'dim_date'[snapshot_date],
    "paths", [Access Paths],
    "risk", [Risk Paths],
    "ppl", [Principals with Access]
  )${filterExprs().map((p) => ",\n  " + p).join("")}
)`
    );
    rows.sort((a, b) => String(pick(a, "[snapshot_date]")).localeCompare(String(pick(b, "[snapshot_date]"))));
    const body = el("div");
    const box = el("div", "chart-box");
    box.style.setProperty("--ch", "260px");
    box.innerHTML = '<canvas id="trendChart"></canvas>';
    body.appendChild(box);
    const c = card("Access surface trend",
      rows.length < 2
        ? "Only one snapshot so far — the trend fills in as the daily pipeline runs."
        : `${rows.length} snapshots captured`,
      body);
    setTimeout(() => {
      drawChart("trendChart", {
        type: "line",
        data: {
          labels: rows.map((r) => pick(r, "[snapshot_date]")),
          datasets: [
            { label: "Access paths", data: rows.map((r) => Number(pick(r, "[paths]")) || 0),
              borderColor: CHART_COLORS[0], backgroundColor: withAlpha(CHART_COLORS[0], .16), fill: true, tension: .3, pointRadius: 3 },
            { label: "Risky paths", data: rows.map((r) => Number(pick(r, "[risk]")) || 0),
              borderColor: THEME.red(), backgroundColor: withAlpha(THEME.red(), .14), fill: true, tension: .3, pointRadius: 3 },
            { label: "Principals", data: rows.map((r) => Number(pick(r, "[ppl]")) || 0),
              borderColor: CHART_COLORS[1], tension: .3, pointRadius: 3 },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          scales: { x: { grid: { display: false } } },
          plugins: { legend: { position: "bottom", labels: { boxWidth: 11, font: { size: 11 } } } },
        },
      });
    }, 0);
    return c;
  }, "Trend");
};

/* ===================================================================== */
/* 7. COMPARE                                                             */
/* ===================================================================== */

const compareState = { a: null, b: null };

PANES.compare = async function (root) {
  root.innerHTML = "";
  root.appendChild(
    card("Compare two identities",
      "Side-by-side entitlement diff — useful for access reviews, joiner/mover/leaver checks, and answering \"why can they see it and I can't?\"",
      null)
  );

  const pickRow = el("div", "grid g2");
  pickRow.style.marginTop = "-6px";
  root.appendChild(pickRow);

  ["a", "b"].forEach((slot) => {
    const box = el("div", "card");
    const label = el("div", "label");
    label.style.cssText = "font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:9px";
    label.textContent = slot === "a" ? "Identity A" : "Identity B";
    box.appendChild(label);
    const cur = el("div");
    cur.style.cssText = "font-size:15px;font-weight:600;margin-bottom:10px";
    cur.textContent = compareState[slot]?.name || "— not selected —";
    box.appendChild(cur);
    box.appendChild(
      searchBox("Search…", loadPrincipals, (r) => {
        compareState[slot] = { id: pick(r, "[principal_id]"), name: pick(r, "[display_name]") };
        PANES.compare(root);
      }, {
        text: (r) => `${pick(r, "[display_name]") || ""} ${pick(r, "[upn]") || ""}`,
        html: (r) =>
          `<div>${escapeHtml(pick(r, "[display_name]"))} <span class="badge b-neutral">${escapeHtml(pick(r, "[principal_type]"))}</span></div>` +
          `<div class="meta">${escapeHtml(pick(r, "[upn]") || "no UPN")} · ${nf(pick(r, "[items]"))} items</div>`,
      })
    );
    pickRow.appendChild(box);
  });

  if (!compareState.a || !compareState.b) {
    root.appendChild(card("", "", el("div", "empty", "Pick two identities to see the difference.")));
    return;
  }

  const out = el("div");
  out.style.marginTop = "14px";
  root.appendChild(out);

  fill(out, async () => {
    const q1 = (pid) => `EVALUATE
CALCULATETABLE(
  SUMMARIZECOLUMNS(
    'dim_item'[item_id],
    'dim_item'[item_name],
    'dim_item'[item_type],
    'dim_item'[workspace_name],
    "perm", MAX('fact_effective_access'[permission_name]),
    "lvl", MAX('fact_effective_access'[permission_level]),
    "path", MAX('fact_effective_access'[access_path]),
    "n", [Access Paths]
  ),
  ${snapshotExpr()},
  'dim_principal'[principal_id] = "${q(pid)}"
)`;
    const [ra, rb] = await Promise.all([runDax(q1(compareState.a.id)), runDax(q1(compareState.b.id))]);
    const mapA = new Map(ra.map((r) => [pick(r, "[item_id]"), r]));
    const mapB = new Map(rb.map((r) => [pick(r, "[item_id]"), r]));
    const all = new Set([...mapA.keys(), ...mapB.keys()]);

    const rows = [...all].map((id) => {
      const a = mapA.get(id), b = mapB.get(id);
      const la = a ? Number(pick(a, "[lvl]")) : -1;
      const lb = b ? Number(pick(b, "[lvl]")) : -1;
      let verdict = "Same";
      if (!a) verdict = "B only";
      else if (!b) verdict = "A only";
      else if (la > lb) verdict = "A stronger";
      else if (lb > la) verdict = "B stronger";
      const src = a || b;
      return {
        "[item_name]": pick(src, "[item_name]"),
        "[item_type]": pick(src, "[item_type]"),
        "[workspace_name]": pick(src, "[workspace_name]"),
        "[perm_a]": a ? pick(a, "[perm]") : null,
        "[perm_b]": b ? pick(b, "[perm]") : null,
        "[path_a]": a ? pick(a, "[path]") : null,
        "[path_b]": b ? pick(b, "[path]") : null,
        "[verdict]": verdict,
        "[sort]": { "A only": 0, "B only": 1, "A stronger": 2, "B stronger": 3, "Same": 4 }[verdict],
      };
    });

    const counts = rows.reduce((m, r) => ((m[pick(r, "[verdict]")] = (m[pick(r, "[verdict]")] || 0) + 1), m), {});
    const wrap = el("div");
    const g = el("div", "grid g5");
    g.appendChild(kpi("Shared items", nf(counts["Same"] || 0), "identical permission"));
    g.appendChild(kpi(`Only ${compareState.a.name}`, nf(counts["A only"] || 0), "B cannot reach these", "warn"));
    g.appendChild(kpi(`Only ${compareState.b.name}`, nf(counts["B only"] || 0), "A cannot reach these", "warn"));
    g.appendChild(kpi("A has more", nf(counts["A stronger"] || 0), "stronger permission on shared items"));
    g.appendChild(kpi("B has more", nf(counts["B stronger"] || 0), "stronger permission on shared items"));
    wrap.appendChild(g);

    const VC = { "A only": "b-write", "B only": "b-build", "A stronger": "b-reshare", "B stronger": "b-reshare", "Same": "b-neutral" };
    const t = card(`Entitlement diff (${nf(rows.length)} items)`,
      `${escapeHtml(compareState.a.name)} vs ${escapeHtml(compareState.b.name)}`,
      dataTable(rows, [
        { key: "[verdict]", label: "Difference", render: (v) => `<span class="badge ${VC[v]}">${escapeHtml(v)}</span>` },
        { key: "[workspace_name]", label: "Workspace" },
        { key: "[item_name]", label: "Item" },
        { key: "[item_type]", label: "Type" },
        { key: "[perm_a]", label: compareState.a.name, render: (v) => v ? permBadge(v) : '<span class="dim">no access</span>' },
        { key: "[perm_b]", label: compareState.b.name, render: (v) => v ? permBadge(v) : '<span class="dim">no access</span>' },
        { key: "[path_a]", label: "A path", render: (v) => v ? renderPath(v) : "—" },
        { key: "[path_b]", label: "B path", render: (v) => v ? renderPath(v) : "—" },
      ], { maxHeight: "540px", sortKey: "[sort]", sortDesc: false }));
    t.style.marginTop = "14px";
    wrap.appendChild(t);
    return wrap;
  }, "Compare");
};

/* ===================================================================== */
/* 8. HEALTH                                                              */
/* ===================================================================== */

PANES.health = async function (root) {
  root.innerHTML = "";

  const kpis = el("div");
  root.appendChild(kpis);
  fill(kpis, async () => {
    const r = await runDax(
      `EVALUATE
CALCULATETABLE(
  ROW(
    "steps", [Pipeline Steps],
    "fail", [Pipeline Failures],
    "health", [Pipeline Health %],
    "last", [Last Refresh],
    "acc", [Model Accuracy],
    "samples", [Validation Samples],
    "cov", [OneLake Coverage %]
  ),
  ${snapshotExpr()}
)`
    );
    const g = el("div", "grid g5");
    const h = val(r, "[health]", 0);
    g.appendChild(kpi("Pipeline health", pctFmt(h),
      `${nf(val(r, "[steps]"))} steps, ${nf(val(r, "[fail]"))} unhealthy`,
      h >= 1 ? "good" : h >= 0.8 ? "warn" : "risk"));
    g.appendChild(kpi("Last refresh", String(val(r, "[last]", "—")).replace("T", " ").slice(0, 19), "UTC"));
    g.appendChild(kpi("Model accuracy", pctFmt(val(r, "[acc]")),
      `validated against ${nf(val(r, "[samples]"))} live API samples`,
      val(r, "[acc]") >= 0.95 ? "good" : "warn"));
    g.appendChild(kpi("OneLake coverage", pctFmt(val(r, "[cov]")), "items the scanner could read"));
    g.appendChild(kpi("Snapshots retained", nf(state.snapshots.length), "daily security snapshots"));
    return g;
  }, "Health KPIs");

  root.appendChild(section("Daily pipeline", "Every extraction and build step, with its outcome"));
  const steps = el("div");
  root.appendChild(steps);
  fill(steps, async () => {
    const rows = await runDax(
      `EVALUATE
CALCULATETABLE(
  SUMMARIZECOLUMNS(
    'fact_pipeline_run'[step_order],
    'fact_pipeline_run'[step],
    'fact_pipeline_run'[status],
    'fact_pipeline_run'[records],
    'fact_pipeline_run'[detail],
    'fact_pipeline_run'[is_healthy],
    "n", [Pipeline Steps]
  ),
  ${snapshotExpr()}
)`
    );
    return card("Pipeline steps", "A step that never logged is reported as NotRun rather than assumed successful",
      dataTable(rows, [
        { key: "[step_order]", label: "#", num: true },
        { key: "[step]", label: "Step" },
        { key: "[status]", label: "Status", render: (v) => {
            const s = String(v || "");
            const cls = s === "Success" ? "b-ok" : s === "NotRun" ? "b-neutral" : "b-risk";
            return `<span class="badge ${cls}">${escapeHtml(s)}</span>`;
          } },
        { key: "[records]", label: "Records", num: true, render: nf },
        { key: "[detail]", label: "Detail", render: (v) => v ? `<span class="trunc" title="${escapeHtml(v)}">${escapeHtml(v)}</span>` : "—" },
      ], { maxHeight: "420px", sortKey: "[step_order]", sortDesc: false }));
  }, "Pipeline steps");

  root.appendChild(section("Validation",
    "OneSafe re-checks itself against the Fabric List Access Entities API on a sample of principals, so silent logic drift surfaces as a falling accuracy score."));
  const vald = el("div");
  root.appendChild(vald);
  fill(vald, async () => {
    const rows = await runDax(
      `EVALUATE
CALCULATETABLE(
  SUMMARIZECOLUMNS(
    'fact_validation'[upn],
    'fact_validation'[api_item_count],
    'fact_validation'[model_item_count],
    'fact_validation'[matched_count],
    'fact_validation'[coverage_pct],
    'fact_validation'[status],
    "n", [Validation Samples]
  ),
  ${snapshotExpr()}
)`
    );
    return card("Reconciliation samples", "Model output vs. the live per-user access API",
      dataTable(rows, [
        { key: "[upn]", label: "Principal" },
        { key: "[api_item_count]", label: "API items", num: true, render: nf },
        { key: "[model_item_count]", label: "OneSafe items", num: true, render: nf },
        { key: "[matched_count]", label: "Matched", num: true, render: nf },
        { key: "[coverage_pct]", label: "Coverage", num: true, render: (v) => `${(Number(v) || 0).toFixed(1)}%` },
        { key: "[status]", label: "Status", render: (v) => `<span class="badge ${String(v) === "OK" ? "b-ok" : "b-reshare"}">${escapeHtml(v)}</span>` },
      ], { maxHeight: "380px", emptyText: "No validation samples in this snapshot." }));
  }, "Validation");

  root.appendChild(section("Scope and limits"));
  const notes = el("div", "card");
  notes.innerHTML = `
    <h3>What OneSafe covers, and what it does not</h3>
    <p class="sub">Being explicit about the boundary matters — an incomplete security picture presented as complete is worse than none.</p>
    <div style="font-size:12.5px;line-height:1.75;color:var(--muted)">
      <b style="color:var(--text)">Covered:</b> workspace role assignments, item-level permissions,
      semantic model Read/Build and RLS role membership, OneLake data access roles with their path rules,
      and transitive Entra group expansion, resolved into a single effective-access fact.<br><br>
      <b style="color:var(--text)">Not covered in this version:</b> activity/usage correlation
      (granted but never used), SQL analytics endpoint T-SQL GRANTs, sensitivity-label enforcement,
      deployment-pipeline and Git permissions, and gateway/connection credentials.<br><br>
      <b style="color:var(--text)">Read this carefully:</b> a permission shown here is what the Fabric
      and Graph APIs report at the time of the last snapshot. It is a strong signal, not a substitute
      for a live authorisation check.
    </div>`;
  root.appendChild(notes);
};
