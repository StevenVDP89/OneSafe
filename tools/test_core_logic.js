/* Unit tests for the pure client-side helpers in onesafe-core.js.
 *
 * The DAX harness proves queries execute, but collapseRows runs *after* the
 * query and decides what an admin actually sees. A bug there would quietly
 * merge two principals' grants or drop a permission - a security tool
 * understating access is worse than one that errors. So it is tested directly,
 * against the exact shape the model returns.
 *
 * Usage: node tools/test_core_logic.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DIST = path.join(__dirname, "..", "app", "dist");

const sandbox = {
  console,
  window: {},
  location: { origin: "https://test.invalid", pathname: "/", hash: "", search: "" },
  document: { getElementById: () => null, createElement: () => ({ style: {} }) },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  msal: { PublicClientApplication: class {} },
  Chart: class {},
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: () => Promise.reject(new Error("no network in unit tests")),
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

// config.js defines window.CONFIG, which core reads at load time.
vm.runInContext(fs.readFileSync(path.join(DIST, "config.js"), "utf8"), sandbox);

// Top-level const in a VM script stays lexically scoped, so export explicitly.
const probe = `
;globalThis.__api = { collapseRows, splitList, permBadges, riskBadges,
                      renderPaths, viaBadges, withAlpha, renderGrantRoutes, PERM_RANK,
                      planeBadge, renderScope, renderRule, dataSecBadges };`;
vm.runInContext(fs.readFileSync(path.join(DIST, "onesafe-core.js"), "utf8") + probe, sandbox);

const { collapseRows, splitList, permBadges, riskBadges, renderPaths, viaBadges, withAlpha,
        renderGrantRoutes, planeBadge, renderScope, renderRule, dataSecBadges } = sandbox.__api;

let failures = 0;
function check(name, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    console.log(`  ok    ${name}`);
  } else {
    failures++;
    console.log(`  FAIL  ${name}\n          expected ${e}\n          actual   ${a}`);
  }
}
function checkThat(name, cond, detail = "") {
  if (cond) console.log(`  ok    ${name}`);
  else {
    failures++;
    console.log(`  FAIL  ${name} ${detail}`);
  }
}

/* ---------------------------------------------------------- splitList */

console.log("\nsplitList");
check("splits and trims", splitList("a; b ;c"), ["a", "b", "c"]);
check("drops blanks", splitList("a;;b;"), ["a", "b"]);
check("dedupes", splitList("a;b;a"), ["a", "b"]);
check("null is empty", splitList(null), []);
check("empty string is empty", splitList(""), []);

/* -------------------------------------------------------- collapseRows */

console.log("\ncollapseRows");

// The exact case reported: one identity, one item, two grants.
const ivana = [
  {
    "[workspace_name]": "Analytics", "[item_name]": "Pipeline_1", "[item_type]": "DataPipeline",
    "[permission_name]": "Admin", "[grant_source]": "WorkspaceRole",
    "[granted_via_name]": null, "[access_path]": "Ivana -> Analytics -> Pipeline_1",
    "[data_plane_restricted]": false, "[risk_flags]": "", "[paths]": 1,
  },
  {
    "[workspace_name]": "Analytics", "[item_name]": "Pipeline_1", "[item_type]": "DataPipeline",
    "[permission_name]": "Reshare", "[grant_source]": "ItemPermission",
    "[granted_via_name]": "Data Team", "[access_path]": "Ivana -> Data Team -> Pipeline_1",
    "[data_plane_restricted]": true, "[risk_flags]": "ItemResharePrivilege", "[paths]": 1,
  },
];

const collapsed = collapseRows(ivana, ["[workspace_name]", "[item_name]", "[item_type]"], {
  merge: ["[permission_name]", "[granted_via_name]", "[access_path]", "[risk_flags]"],
  sum: ["[paths]"],
  any: ["[data_plane_restricted]"],
});

check("two grants become one row", collapsed.length, 1);
check("permissions concatenated", collapsed[0]["[permission_name]"], "Admin;Reshare");
check("risk flags carried", collapsed[0]["[risk_flags]"], "ItemResharePrivilege");
check("paths summed", collapsed[0]["[paths]"], 2);
check("route count recorded", collapsed[0]["[routes]"], 2);
checkThat("restricted is true if any route is", collapsed[0]["[data_plane_restricted]"] === true);
check("both access paths kept", splitList(collapsed[0]["[access_path]"]).length, 2);
check("key columns preserved", collapsed[0]["[item_name]"], "Pipeline_1");

// Distinct items must never be merged - the failure that would hide access.
const twoItems = collapseRows(
  [
    { "[item_name]": "A", "[permission_name]": "Read", "[paths]": 1 },
    { "[item_name]": "B", "[permission_name]": "Admin", "[paths]": 1 },
  ],
  ["[item_name]"],
  { merge: ["[permission_name]"], sum: ["[paths]"] }
);
check("distinct items stay separate", twoItems.length, 2);
check("item A keeps only its own permission", twoItems[0]["[permission_name]"], "Read");
check("item B keeps only its own permission", twoItems[1]["[permission_name]"], "Admin");

// A null key must not collide with a different null-keyed entity.
const nullKeys = collapseRows(
  [
    { "[ws]": null, "[item]": "X", "[permission_name]": "Read", "[paths]": 1 },
    { "[ws]": null, "[item]": "Y", "[permission_name]": "Admin", "[paths]": 1 },
  ],
  ["[ws]", "[item]"],
  { merge: ["[permission_name]"], sum: ["[paths]"] }
);
check("null key parts do not collide", nullKeys.length, 2);

// Risk flags arrive already ";"-joined; the union must flatten, not nest.
const flags = collapseRows(
  [
    { "[item]": "X", "[risk_flags]": "GuestAccess;BroadGroupGrant", "[paths]": 1 },
    { "[item]": "X", "[risk_flags]": "BroadGroupGrant;OrphanedPrincipal", "[paths]": 1 },
  ],
  ["[item]"],
  { merge: ["[risk_flags]"], sum: ["[paths]"] }
);
check("risk flags union without duplicates",
  splitList(flags[0]["[risk_flags]"]).sort(),
  ["BroadGroupGrant", "GuestAccess", "OrphanedPrincipal"]);

check("empty input yields empty output", collapseRows([], ["[a]"], {}), []);

// Totals must survive collapsing, or the KPI cards and the table disagree.
const many = [];
for (let i = 0; i < 50; i++) {
  many.push({ "[item]": `item${i % 7}`, "[permission_name]": "Read", "[paths]": 3 });
}
const manyOut = collapseRows(many, ["[item]"], { merge: ["[permission_name]"], sum: ["[paths]"] });
check("collapses to distinct key count", manyOut.length, 7);
check("sum is conserved", manyOut.reduce((s, r) => s + r["[paths]"], 0), 150);
check("route count is conserved", manyOut.reduce((s, r) => s + r["[routes]"], 0), 50);

/* --------------------------------------------------- collapseRows: pair mode */

// The whole point of pair mode is that a permission stays attached to the route
// that granted it. Independent merging loses that and is what the UI showed
// before: a bag of permissions next to an unrelated bag of paths.
console.log("\ncollapseRows pair mode");
const paired = collapseRows(
  [
    { "[item]": "Pipeline_1", "[permission_name]": "Admin", "[granted_via_name]": null,
      "[access_path]": "Workspace Sales (Admin) -> Pipeline_1" },
    { "[item]": "Pipeline_1", "[permission_name]": "Reshare", "[granted_via_name]": "Finance",
      "[access_path]": "Group Finance -> Pipeline_1" },
  ],
  ["[item]"],
  { merge: ["[permission_name]"],
    pair: { key: "[grant_routes]", cols: ["[permission_name]", "[granted_via_name]", "[access_path]"] } }
);
check("pair collapses to one row", paired.length, 1);
check("pair keeps one tuple per route", paired[0]["[grant_routes]"].length, 2);
check("Admin stays with its own path",
  paired[0]["[grant_routes]"].find((r) => r["[permission_name]"] === "Admin")["[access_path]"],
  "Workspace Sales (Admin) -> Pipeline_1");
check("Reshare stays with its own group",
  paired[0]["[grant_routes]"].find((r) => r["[permission_name]"] === "Reshare")["[granted_via_name]"],
  "Finance");
check("summary permission column still merged", paired[0]["[permission_name]"], "Admin;Reshare");

// Identical routes repeated by the query must not reappear as visual duplicates.
const dupRoutes = collapseRows(
  [
    { "[item]": "X", "[permission_name]": "Read", "[granted_via_name]": "G", "[access_path]": "G -> X" },
    { "[item]": "X", "[permission_name]": "Read", "[granted_via_name]": "G", "[access_path]": "G -> X" },
    { "[item]": "X", "[permission_name]": "Write", "[granted_via_name]": "G", "[access_path]": "G -> X" },
  ],
  ["[item]"],
  { pair: { key: "[r]", cols: ["[permission_name]", "[granted_via_name]", "[access_path]"] } }
);
check("identical routes deduped", dupRoutes[0]["[r]"].length, 2);
check("raw route count still reported", dupRoutes[0]["[routes]"], 3);

/* ------------------------------------------------------------ renderers */

console.log("\nrenderers");
const badges = permBadges("Read;Admin;Build");
checkThat("permBadges orders strongest first",
  badges.indexOf("Admin") < badges.indexOf("Build") &&
  badges.indexOf("Build") < badges.indexOf("Read"), badges);
checkThat("permBadges renders one badge per permission",
  (badges.match(/class="badge/g) || []).length === 3);
checkThat("permBadges handles empty", permBadges("").includes("—"));
checkThat("riskBadges shows clean when no flags", riskBadges("").includes("clean"));
checkThat("riskBadges renders each flag",
  (riskBadges("GuestAccess;BroadGroupGrant").match(/class="badge/g) || []).length === 2);
checkThat("renderPaths emits one line per path",
  (renderPaths("A -> B;C -> D").match(/path-line/g) || []).length === 2);
checkThat("renderPaths handles empty", renderPaths(null).includes("—"));
checkThat("viaBadges falls back to direct", viaBadges(null).includes("direct"));
checkThat("viaBadges renders groups",
  (viaBadges("Team A;Team B").match(/b-group/g) || []).length === 2);

const routesHtml = renderGrantRoutes(paired[0]["[grant_routes]"]);
checkThat("renderGrantRoutes emits one block per route",
  (routesHtml.match(/class="route"/g) || []).length === 2, routesHtml);
checkThat("renderGrantRoutes orders strongest permission first",
  routesHtml.indexOf("Admin") < routesHtml.indexOf("Reshare"), routesHtml);
checkThat("renderGrantRoutes labels a direct grant", routesHtml.includes("direct"));
checkThat("renderGrantRoutes labels a group grant", routesHtml.includes("Finance"));
checkThat("renderGrantRoutes shows the path", routesHtml.includes("Pipeline_1"));
checkThat("renderGrantRoutes handles empty", renderGrantRoutes([]).includes("—"));
checkThat("renderGrantRoutes handles null", renderGrantRoutes(null).includes("—"));

// User-controlled strings reach innerHTML, so escaping is a security property.
checkThat("permBadges escapes html", !permBadges('<img src=x onerror=1>').includes("<img"));
checkThat("renderPaths escapes html", !renderPaths('<script>alert(1)</script>').includes("<script"));
checkThat("riskBadges escapes html", !riskBadges('<b>x</b>').includes("<b>x"));
checkThat("renderGrantRoutes escapes html",
  !renderGrantRoutes([{ "[permission_name]": "Read", "[granted_via_name]": "<b>g</b>",
                        "[access_path]": "<script>alert(1)</script>" }]).includes("<script"));

console.log("\nalpha");
check("hex to rgba", withAlpha("#14a67f", 0.5), "rgba(20,166,127,0.5)");
check("passes through non-hex", withAlpha("var(--x)", 0.5), "var(--x)");

console.log("\nrow & column security renderers");
checkThat("planeBadge labels the model plane", planeBadge("SemanticModel").includes("model"));
checkThat("planeBadge labels the OneLake plane", planeBadge("OneLake").includes("OneLake"));
checkThat("planeBadge handles blank", planeBadge("").includes("—"));

const scopeHtml = renderScope("/Tables/dbo/customer", "TaxId;ContactEmail");
checkThat("renderScope shows the table", scopeHtml.includes("/Tables/dbo/customer"));
checkThat("renderScope chips each column", (scopeHtml.match(/class="chip"/g) || []).length === 2);
checkThat("renderScope falls back when unscoped", renderScope("", "").includes("whole item"));
checkThat("renderScope escapes html",
  !renderScope('<b>t</b>', '<i>c</i>').includes("<b>t"));

const ruleHtml = renderRule("Region = AMER", "SELECT * FROM dbo.customer WHERE Region = 'AMER'");
checkThat("renderRule shows the summary", ruleHtml.includes("Region = AMER"));
// The verbatim expression is the reviewable artefact - it must never be dropped.
checkThat("renderRule shows the full expression", ruleHtml.includes("dbo.customer"));
checkThat("renderRule does not repeat an identical expression",
  (renderRule("x", "x").match(/x/g) || []).length === 1);
checkThat("renderRule handles empty", renderRule("", "").includes("—"));
checkThat("renderRule escapes html",
  !renderRule("s", '<script>alert(1)</script>').includes("<script"));

checkThat("dataSecBadges shows RLS", dataSecBadges(true, false, "R1").includes("RLS"));
checkThat("dataSecBadges shows CLS", dataSecBadges(false, true, "R1").includes("CLS"));
checkThat("dataSecBadges shows both", (() => {
  const h = dataSecBadges(true, true, "R1;R2");
  return h.includes("RLS") && h.includes("CLS");
})());
checkThat("dataSecBadges is blank when unrestricted", dataSecBadges(false, false, "").includes("—"));
checkThat("dataSecBadges escapes role names in the tooltip",
  !dataSecBadges(true, false, '<b>r</b>').includes("<b>r</b>"));

console.log(
  failures ? `\n${failures} test(s) FAILED` : "\nAll core logic tests passed."
);
process.exit(failures ? 1 : 0);
