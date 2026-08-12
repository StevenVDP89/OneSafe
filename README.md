# OneSafe

A security 360° single pane of glass for Microsoft Fabric.

OneSafe answers one question that Fabric itself cannot: **what can this identity
actually reach, and how?** It resolves that across all three planes where Fabric
security is defined, for every user, group, service principal and managed
identity in the tenant.

| Plane | What lives there | Why it isn't enough on its own |
|---|---|---|
| Workspace | Admin / Member / Contributor / Viewer role assignments | Says nothing about individual items, and most grants arrive through groups |
| Item | Per-item permissions, semantic-model Read vs Build, RLS role membership, app audiences | Invisible unless you open each item, one at a time |
| Data (OneLake Security) | Data access roles on Lakehouses, with folder/table/row/column scoping | Can silently *narrow* access that the other two planes appear to grant |

A grant to a security group in the workspace plane, an RLS role in the item
plane, and a OneLake role in the data plane all combine to decide what one
person sees. OneSafe expands the groups, unions the planes, keeps the strongest
permission, and records the path it took to get there.

---

## Deploying it

OneSafe is designed to be deployed into any Fabric tenant, not just the one it
was built in. Nothing in this repository is tenant-specific: every id is
discovered at setup time and written to `tools/config.json`, which is
gitignored.

```powershell
git clone <this-repo> OneSafe
cd OneSafe
az login --tenant <your-tenant-id>

python tools/setup.py --check     # verify prerequisites, change nothing
python tools/setup.py             # provision everything
```

**→ [`docs/SETUP.md`](docs/SETUP.md) is the full deployment guide.** Read it
before running the script — two of the required steps (Entra admin consent and
four Fabric tenant settings) have no API and must be done by a tenant admin in
the portal. `setup.py` checks for them and tells you exactly what to click, but
it cannot do them for you.

`setup.py` is find-or-create throughout: re-running it is safe and is the
intended way to recover from a partial failure. It never deletes anything, and
it preserves existing capacity assignments, redirect URIs and client secrets
unless you explicitly ask otherwise.

You will need Python 3.9+ (no third-party packages), the Azure CLI, Node.js, and
a Fabric capacity — Direct Lake does not work on Power BI Premium SKUs.

> OneSafe's own model is a map of every weak point in your tenant. Lock the
> workspace, the semantic model and the app to a tenant-admin group before you
> share the URL. `tools/secure_model.py` does most of this; step 8 of the setup
> guide covers the rest.

---

## What is deployed

```
Fabric REST · Power BI Scanner · OneLake dataAccessRoles · Microsoft Graph
      │  service principal, daily
      ▼
  10 PySpark notebooks
      │
   bronze  raw API payloads, append-only, snapshot-dated
      ▼
   silver  typed, normalised entities
      ▼
   gold    star schema + fact_effective_access
      ▼
  sm_onesafe   Direct Lake semantic model (18 tables, ~51 measures)
      ▼
  OneSafe app  Rayfin-hosted SPA, 9 panes, all visuals hand-built on live DAX
```

| Component | Where |
|---|---|
| Data workspace | **OneSafe** — lakehouse, notebooks, pipeline, semantic model |
| App workspace | **OneSafe App** — Rayfin item + static front-end |
| Lakehouse | `lh_onesafe`, schemas `bronze` / `silver` / `gold` |
| Semantic model | `sm_onesafe` (Direct Lake) |
| Pipeline | `pl_onesafe_daily`, scheduled 04:30 UTC |
| App URL | printed by `npx rayfin up` — `https://<name>-<region>.webapp.fabricapps.net` |
| Admin group | `OneSafe Administrators` |

The app is deliberately in a **separate workspace** from the data. Rayfin item
creation is unavailable on some capacities, and more importantly, OneSafe's
model is a map of every weak point in the tenant — the front-end should not
carry rights over the lakehouse that produced it.

---

## The data model

### Grain that matters

- **`fact_effective_access`** — one row per *access path*. If someone can reach
  an item three different ways, that is three rows. This is what makes the
  question "why does she have access?" answerable rather than merely "does she".
- **`fact_access_summary`** — one row per (principal, item) holding the
  strongest permission plus `path_count`. Use this for counting; use the other
  for explaining.
- **`fact_data_security`** — one row per row/column rule *per assigned
  principal*, unifying two planes that are configured in completely different
  places: semantic-model RLS/CLS roles and OneLake data access role constraints.
  Permissions answer "can they open it"; this answers "what do they see once
  they do".

Keeping both is the point. Collapsing to one loses the explanation; keeping only
paths makes every count wrong by a multiple.

The tables in the app collapse the path grain **for display only**: one row per
principal-and-item, with permissions and risk flags concatenated and a `Routes`
count showing how many underlying paths were folded in. Two rows for
"Ivana → Pipeline_1, Admin" and "Ivana → Pipeline_1, Reshare" are correct data
and misleading presentation — the eye reads duplication rather than two distinct
grant routes. The card subtitle still reports the raw route total, so nothing
looks lost, and clicking through re-expands. This happens client-side in
`collapseRows`; the model is untouched.

### Permission scale

`None 0 · Read 1 · Build/Explore 2 · Write 3 · Reshare 4 · Admin/Owner 5`

Effective permission is the maximum across all paths — matching how Fabric
actually evaluates access.

### Risk flags

`GuestAccess` · `OrphanedPrincipal` (disabled account still holding access) ·
`ServicePrincipalWriteAccess` · `ItemResharePrivilege` · `BroadGroupGrant` ·
`GroupGrantOnSecuredData` · `RlsBypassedByWriteAccess`

`RlsBypassedByWriteAccess` is the one worth explaining: row filters constrain
*reading*. A principal with Write or higher on the item can edit or remove the
filter, so the restriction is decorative for them. It looks like security in the
portal and is not.

### Row- and column-level security

Two planes, one table. They are configured through entirely different surfaces
and neither is visible in a permissions list:

| Plane | Where it lives | How OneSafe reads it |
|---|---|---|
| Semantic model | `roles[].tablePermissions[]` in the model's TMSL | `getDefinition?format=TMSL` for the rules, **XMLA/TOM** for the members |
| OneLake | `decisionRules[].constraints` on a data access role | `dataAccessRoles` returns rules and members together |

The semantic-model split is not a design choice, and finding the right second
source took some doing. **The definition API strips `roles[].members[]` on both
read and write**, and there is no REST endpoint for RLS role membership at all.
The Scanner API documents a `rowLevelSecurity[].members[]` field, so that was the
obvious candidate — but across a full scan of this tenant it appeared **zero**
times, while TOM showed the members sitting in the model the whole time
(`notebooks/99_check_rls_members.py` is the diagnostic that settled it, and is
worth keeping for the next tenant that disagrees).

So `03_extract_onelake.py` opens an XMLA connection per workspace and reads
`Model.Roles[].Members` directly. It only does this for models that actually
declare roles — a model with no roles has no membership to miss — which keeps a
tenant-wide pass cheap. Rules and members are then joined on
`(item_id, role_name)`. TOM reports members as `upn#AzureAD`; the Entra object id
comes from `MemberID` and is what everything downstream joins on.

This has two costs worth knowing about: **XMLA read must be enabled on the
capacity**, and a workspace whose XMLA endpoint is unreachable is recorded with
`roleMemberStatus = "Error"` rather than being silently reported as having no
members.

A filter mentioning `USERPRINCIPALNAME()`, `USERNAME()` or `CUSTOMDATA()` is
flagged `is_dynamic` — it resolves per signed-in user, so it cannot be reviewed
by reading the expression alone.

A rule with no members is kept with a null principal on purpose. An unassigned
role restricts nobody, which is a finding, not an absence.

### Dimensions

Dimensions hold **one row per entity**, with `first_seen_date` / `last_seen_date`.
History lives in the facts, which are snapshot-dated. Doing it the other way
round — a dimension row per entity per day — silently multiplies every distinct
count in the model.

---

## The pipeline

| Step | Notebook | Does |
|---|---|---|
| 01 | `01_extract_inventory` | Workspaces, capacities, workspace users, tenant settings |
| 02 | `02_extract_scanner` | Power BI Scanner API — items, per-item users, lineage |
| 03 | `03_extract_onelake` | `dataAccessRoles` per Lakehouse; model RLS/CLS rules via TMSL and role members via XMLA |
| 04 | `04_extract_graph` | Graph principals + transitive group membership |
| 05 | `05_transform_silver` | Parse bronze JSON into typed Delta |
| 06 | `06_build_gold` | Dims, bridges, facts, effective-access resolution |
| 07 | `07_build_changes` | Day-over-day diff → `fact_access_change` |
| 08 | `08_validate` | Reconcile a sample against `List Access Entities` |
| 09 | `09_refresh_model` | Sync SQL endpoint, refresh the model, publish telemetry |
| 10 | `10_on_failure` | Failure handler |

Step 07 reports **Skipped** until a second daily snapshot exists — there is
nothing to diff on day one. That is correct behaviour, not a fault.

### Two things step 09 exists to prevent

1. **Spark tables are invisible to the SQL analytics endpoint until its metadata
   is synced.** Without the sync, DAX fails with
   `Invalid object name 'gold.fact_effective_access'` even though the Delta
   tables are sitting right there.
2. **Telemetry written after the refresh would always describe the previous
   run.** So `fact_pipeline_run` is written, then that single table is re-framed.
   A health table that is a day stale is worse than none.

### Refresh identity

The Power BI refresh endpoint is governed by a *different* tenant setting than
the read-only admin APIs the rest of OneSafe uses. In many tenants — including
this one — service principals are refused with
`API is not accessible for application`. Step 09 therefore tries the service
principal, and on 401/403 falls back to the identity executing the notebook,
which in a scheduled run is the pipeline owner. `fact_pipeline_run.detail`
records which identity was used.

To move the refresh onto the service principal, enable *"Service principals can
use Power BI APIs"* for a group containing `OneSafe-Scanner`.

---

## Prerequisites

**Entra app `OneSafe-Scanner`** with admin-consented Graph application
permissions: `User.Read.All`, `Group.Read.All`, `Directory.Read.All`,
`Application.Read.All`.

**Fabric tenant settings**, each enabled for a security group containing the SPN:

- Service principals can call Fabric public APIs
- Service principals can access read-only admin APIs
- Enhance admin APIs responses with detailed metadata
- Enhance admin APIs responses with DAX and mashup expressions

The last two are what make the Scanner API return per-item user lists. Without
them the scan succeeds and returns almost nothing useful — a quiet failure.

`python tools/setup.py --check` reports which of the four are currently enabled.
There is no write API for tenant settings, so they must be toggled in the portal.

**Do not** assign admin-consent-required Power BI permissions to the SPN in
Entra. Power BI authorises service principals through the tenant settings above;
adding the Entra permissions actively breaks admin API access.

### API limits designed around

| API | Limit | Handling |
|---|---|---|
| Scanner | 500 req/hour, 16 concurrent, 100 workspaces/request | Chunked, throttled, checkpointed so a partial run resumes |
| List Access Entities | 200 req/hour | Sampled validation only, never a primary source |
| All | 429 + `Retry-After` | Exponential backoff in `api_request` |

---

## Credentials

The scanner's client secret lives in `Files/config/onesafe_config.json` inside
the OneSafe lakehouse — the same trust boundary as the security data itself.

Key Vault would be preferable, and `00_common` is structured so switching to
`notebookutils.credentials.getSecret` is a small change. It is not used here
because a tenant Azure Policy forces Key Vault public network access off, and
reaching it from Fabric would require a managed private endpoint.

```powershell
python tools\upload_config.py <client-secret>   # full write, rotating the secret
python tools\upload_config.py --sync            # merge IDs, keep stored secret
```

`--sync` re-reads the stored secret rather than asking for it, so adding an item
ID never puts the credential in shell history.

> `tools/config.json` must stay **BOM-free**. PowerShell's `Out-File` adds one,
> which breaks `json.loads`. Readers use `utf-8-sig` defensively; writers should
> use `[IO.File]::WriteAllText` with `UTF8Encoding($false)`.

---

## The app

Nine panes, all cross-filtering through shared server-side predicates — one
click narrows every pane, because each composes its DAX from the same filter
state rather than filtering in the browser.

| Pane | Answers |
|---|---|
| Overview | Tenant posture: KPIs, risk tiles, where access concentrates |
| Principal 360 | One identity → every workspace, item, permission, path, OneLake restriction |
| Item 360 | One item → everyone who can reach it, how, at what level |
| Access Graph | Interactive principal ↔ group ↔ workspace ↔ item network; click to re-pivot |
| OneLake Security | Lakehouses with data access roles, rules, and coverage gaps |
| Row & column security | Every RLS/CLS rule across both planes, verbatim, and who it applies to |
| Risk & Drift | Guests, orphaned accounts, SPN write access, broad grants, change feed |
| Compare | Two identities side by side, or one identity across two dates |
| Health | Pipeline telemetry, validation accuracy, freshness |

### Testing the front-end

The panes build their DAX at runtime from filter state, so a query can break in
ways reading cannot catch. Signing in and clicking nine tabs tests exactly one
filter combination.

```powershell
node tools\capture_app_dax.js     # run panes headlessly, capture every query
python tools\check_app_dax.py     # execute each against the live model
node tools\test_core_logic.js     # unit-test the pure rendering helpers
node tools\test_graph_render.js   # prove the graph canvas actually draws
node tools\verify_signin.js       # fetch deployed assets, boot MSAL, print redirect URI
```

`capture_app_dax.js` drives all 9 panes across 4 filter states (unfiltered,
principal-focused, item-focused, stacked) and currently emits 110 queries;
`check_app_dax.py` runs every one against the live model. Empty results are
reported separately from failures, because a pane that legitimately has nothing
to show and a pane whose query is broken look identical in the browser.

`test_core_logic.js` covers the helpers the DAX harness structurally cannot
reach: in the harness `runDax` returns `[]`, so every row-shaping function is
never exercised. It asserts `collapseRows`, `splitList`, `permBadges`,
`riskBadges`, `renderPaths`, `viaBadges`, `renderGrantRoutes`, `renderScope`,
`renderRule`, `dataSecBadges` and `withAlpha`, including that route counts and
numeric totals are conserved when rows merge, that two different partial keys
cannot collide, and that every renderer escapes HTML — these write straight into
`innerHTML` with values sourced from item and principal names.

`test_graph_render.js` exists because of a bug neither other suite could see.
`PANES.graph` declared a local `alpha` for the force-layout cooling factor, which
shadowed a global colour helper of the same name; the resulting `TypeError` was
thrown **inside a `requestAnimationFrame` callback**, which fails completely
silently — no console error, no UI change, just a blank canvas. The test drives
the pane against a recording 2D context and fails if no arcs or strokes are ever
issued. The global helper is now `withAlpha`; keep it that way, and keep every
rAF loop wrapped in a try/catch that surfaces the error visibly.

`verify_signin.js` exists because the one failure mode neither of the others can
see is a script that never loads. The app originally pulled MSAL from a CDN URL
that returned 404, which surfaced only as `_msal is not defined` — every local
file was correct. It now fetches what the host actually serves, asserts no
script comes from an external origin, constructs `PublicClientApplication`
exactly as the page does, and prints the derived `redirectUri` so it can be
compared against the Entra registration.

This runs all 9 panes under 4 filter states plus the page bootstrap
(`loadSnapshots`, `loadFreshness`), deduplicates, and executes every unique
query. Run it after any change to the model **or** the panes — a renamed
column shows up here as a failure rather than as an empty panel that an admin
has to notice and quietly distrust.

Empty results are reported separately from failures: several are legitimate
(no access changes before a second snapshot; synthetic IDs in the harness).

---

## Operating it

```powershell
python tools\deploy_notebooks.py        # push notebooks (00_common is inlined)
python tools\run_notebooks.py 06_build_gold   # run one step
python tools\run_pipeline.py            # run the whole pipeline now
python tools\build_semantic_model.py    # redeploy the semantic model
python tools\query_model.py -f q.dax    # ad-hoc DAX
python tools\run_notebooks.py 99_cleanup      # remove stray run_log rows, rebuild telemetry
```

`99_cleanup` is deliberately not wired into the pipeline. It exists because
telemetry is derived from an append-only `bronze.run_log`, so a hand-run or
mistaken step stays visible until its rows are deleted and the gold table is
rebuilt. Edit its `DELETE` predicate to suit, then run it.

### When a notebook fails

Fabric gives **no cell-level detail** for a failed notebook job — the job status
is `Failed` and nothing more. `00_common` installs an error trap that writes the
full traceback to OneLake:

```powershell
python tools\read_onelake.py Files/diag/error_09_refresh_model.txt
```

This is the primary debugging path. Reach for it first.

### Reading pipeline health

`gold.fact_pipeline_run` carries one row per canonical step per day.

- Notebooks log fine-grained names (`extract_inventory.workspaces`); these are
  mapped to canonical steps via `STEP_ALIASES`. Unrecognised names are kept
  under their own name rather than dropped, so a new notebook appears rather
  than vanishing.
- A step attempted twice in a day reports its **latest** attempt. A successful
  rerun should turn the dashboard green — otherwise the failure it fixed masks
  it forever.
- Within one attempt, the **worst** sub-step wins, so one failure cannot be
  hidden by a later success in the same notebook.
- Steps that never reported are emitted as `NotRun`, which is what makes a
  hard crash visible.

---

## Gotchas worth knowing before changing anything

- **`SUMMARIZECOLUMNS` without a measure crossjoins unrelated dimensions.**
  Always include one. `CALCULATETABLE(SUMMARIZECOLUMNS(...), <bool filters>)` is
  the pattern the whole app is built on.
- **`Sql.Database(server, database)` in Direct Lake M takes the SQL analytics
  endpoint ID**, not the lakehouse ID.
- **Fabric item create/update is long-running**: `202` + `Location`, poll until
  `Succeeded`.
- **Pipeline `dependsOn` with multiple entries is AND**, so the failure branch
  needs one handler per step.
- **`notebookutils.fs.head` silently truncates.** Never use it for bronze reads.
- **Spark `.alias()` with a USING-list join** raises `MISSING_ATTRIBUTES`. Use
  explicit join conditions.
- **Capacities come from `GET /v1.0/myorg/admin/capacities`.** The Fabric
  equivalent `/v1/admin/capacities` returns 404.
- **Schedules use `configuration.localTimeZoneId`**, not `localTimeZone`.
- **Semantic models are not exposed to the Fabric jobs API** — refresh goes
  through the Power BI REST API.
- **Never `tolerate` a status code on a security-relevant read.** `api_request`
  can turn a failing call into `None`, which reads downstream as "asked, found
  nothing". For inventory that is fine; for permissions it silently converts
  *unknown* into *none* and the tool then reports safety it never verified.
  `03_extract_onelake` did exactly this for a year of 400/401/403 responses and
  reported 42/42 coverage when the real figure was 1. Catch, classify, record.
- **OneLake Security is enabled per lakehouse**, not per tenant or workspace,
  despite the error naming the workspace
  (`UniversalSecurityFeatureDisabledForWorkspace`). Older lakehouses have it off
  and cannot hold data access roles until it is switched on.
- **OneLake data access role names must be alphanumeric** and start with a
  letter. Underscores are rejected with `RequestBodyValidationFailed`.
- **Writing data access roles is a whole-collection PUT.** Read the existing
  roles first and echo back the ones you are not changing — including the
  built-in `DefaultReader` — or they are deleted. Pass the collection `ETag`
  as `If-Match`.
- **Delta append fails when a new column appears.** `append_snapshot` sets
  `mergeSchema` so an evolving REST payload widens the table instead of breaking
  the nightly run.
- **Measure names in a tabular model are model-wide, not table-scoped.** A
  collision is only reported *after* a full deploy round-trip, in a message that
  names one measure and no location — so you learn about it several minutes
  later, with no clue which of the two is the newcomer.
  `build_semantic_model.py` now checks for duplicates locally and refuses to
  deploy, which turns a slow, confusing failure into an instant, specific one.
- **A Fabric notebook *job* produces no cell output**, and an unhandled exception
  cancels the session before any later cell runs. Diagnostics must write to
  `Files/…` from a `finally` block and be read back with `tools/read_onelake.py`,
  or they will only ever report success.
- **Entra matches redirect URIs exactly.** The app derives its redirect URI from
  the live pathname so localhost and the hosted origin work from one build, but
  it normalises `/index.html` to `/` first — otherwise every reachable spelling
  of the same page would need its own registration.
- **The MSAL and Chart.js bundles are vendored under `app/dist/vendor/`**, not
  loaded from a CDN. A CDN 404 is invisible in source review and surfaces only
  as `_msal is not defined` at runtime. Keep them local.

---

## Known gaps

- **OneLake Security coverage depends on the scanner SPN's workspace access.**
  Unlike the admin APIs, `dataAccessRoles` is a *data-plane* call: it answers
  only for workspaces the SPN is actually a member of. In this tenant that is
  currently 2 of 42 OneLake-capable items, and the remaining 40 report
  `coverage_status = AccessDenied`.

  This used to be invisible. The extractor tolerated the failing status codes
  and recorded those items as "read successfully, 0 roles", so the app confidently
  reported near-total coverage and no OneLake security anywhere. Both statements
  were wrong. `coverage_status` now distinguishes:

  | status | meaning |
  |---|---|
  | `Ok` | read; `role_count` is the truth |
  | `FeatureDisabled` | OneLake Security has never been switched on for that item, so scoping is impossible |
  | `AccessDenied` | the SPN is not a member of that workspace — **unknown**, not "none" |
  | `NotSupported` | item type does not expose data access roles |
  | `Error` | anything else, with the message retained |

  To widen coverage, add the scanner SPN to more workspaces (a tenant-wide group
  granted Viewer on each workspace is the usual approach). Nothing else changes;
  the numbers simply become complete. **Never treat `AccessDenied` as "no
  restrictions" — for a security tool, unknown and none must not look alike.**
- **Semantic-model RLS membership depends on XMLA read.** It is the only surface
  that reports it (see *Row- and column-level security*), so a capacity with the
  XMLA read endpoint disabled will show RLS *rules* with no members against them.
  These are recorded as `roleMemberStatus = "Error"`, not as empty roles — the
  same unknown-is-not-none rule as OneLake coverage. It is also the one part of
  extraction that scales per workspace rather than per API page; on a very large
  tenant it is the first thing to parallelise further.
- **`fact_access_change` is empty until a second daily snapshot exists.**
- **`dim_principal` holds apparent duplicates** where an app registration and
  its service principal both appear. They are genuinely distinct Entra objects
  with distinct IDs; type-grouped counts will show both.
- **Scale is untested beyond 31 workspaces.** The throttling, chunking and
  checkpointing are built for more, but have not met a large tenant.

### The OneSafe Demo sandbox

OneLake Security is enabled *per lakehouse*, and none of the pre-existing
lakehouses in this tenant had it on — the API answers
`UniversalSecurityFeatureDisabledForWorkspace` for them. So there was nothing
for the OneLake pane to show.

`tools/seed_onelake_roles.py` provisions a demonstrable example rather than
altering anyone else's security posture:

- workspace **OneSafe Demo** (`e490d04c-…`), lakehouse **lh_onesafe_demo**
- Ilaria and Ivana are workspace Viewers — that is what gives them the item
  access these roles then scope
- three path-scoped roles: `OneSafeDemoSalesReader` (Ilaria),
  `OneSafeDemoSentimentReader` (Ivana), `OneSafeDemoSharedReader` (both) — giving
  the app a multi-member role, a principal in multiple roles, and distinct path
  scopes to traverse
- three constraint-bearing roles exercising the data plane's own RLS/CLS:
  `OneSafeDemoCustomerNoPII` (columns), `OneSafeDemoCustomerAMER` (rows),
  `OneSafeDemoFinanceRestricted` (both)

It is deliberately **not** seeded against `lh_onesafe`: that lakehouse maps every
weak point in the tenant, and naming ordinary users in roles on it is the wrong
default for a security tool. Run `--list` to inspect, `--remove` to tear down.
Role names must be alphanumeric — the API rejects underscores.

**OneLake row filters are validated against the real table schema.** Three
things must line up or the PUT fails with `InvalidRLSPredicate`, which names the
path and nothing else:

1. the table must actually exist — `notebooks/97_seed_demo_lakehouse.py` creates
   the small demo tables purely so the rules have something to validate against;
2. the `FROM` clause must be schema-qualified (`dbo.customer`, not `customer`),
   even though the `tablePath` already names the schema;
3. string literals must use single quotes — double quotes parse as identifiers.

Semantic-model RLS/CLS is seeded separately, because it lives in a different
place and behaves differently:

- `tools/seed_demo_model.py` creates **`sm_onesafe_demo`** with four roles:
  two static row filters, one pure column filter, and one dynamic
  (`USERPRINCIPALNAME()`) role that also hides a column.
- `notebooks/98_seed_demo_rls.py` attaches the *members*, over XMLA/TOM. This is
  not a stylistic choice: the definition API silently strips `roles[].members[]`
  on write, so members deployed through it simply never arrive. `TOM` is the only
  writable surface, and it only works from inside a Fabric notebook.
- `notebooks/99_check_rls_members.py` is the diagnostic for when membership does
  not appear downstream — it reads silver, the raw scanner payload and TOM side
  by side and writes the comparison to `Files/diag/rls_check.txt`. It is what
  proved the Scanner API never reports RLS members. Because a notebook *job*
  produces no cell output and any raised exception cancels the session, it runs
  as a single cell with every probe individually guarded and the report written
  in a `finally` — a diagnostic that only reports when nothing is wrong is
  worthless.

## Phase 2 candidates

Activity events (dormant-permission detection: granted but never used) ·
SQL analytics endpoint GRANTs · Purview sensitivity labels · deployment pipeline
access · gateway and connection permissions.
