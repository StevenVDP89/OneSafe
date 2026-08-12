# Deploying OneSafe into your own tenant

This guide takes a Fabric tenant with nothing in it to a working OneSafe
deployment: daily pipeline, Direct Lake semantic model, and the admin front-end.

Budget about **45 minutes**, most of which is the first pipeline run and waiting
for a Global Administrator to click *Grant admin consent*.

> **Read this first.** OneSafe builds a map of every access path in your tenant.
> That model is more sensitive than most of the data it describes: it is a list
> of where the weak points are. Treat the workspace, the semantic model and the
> app as tenant-admin-only from the moment you create them. Step 8 covers this.

---

## What you need before you start

| | Why |
|---|---|
| **Fabric tenant administrator** | Four tenant settings have no API and must be toggled in the portal |
| **Entra privileges to create app registrations** | The scanner identity and the front-end sign-in app |
| **A Global / Privileged Role Administrator** | Admin consent for the scanner's Graph permissions. Can be someone else — the script prints the exact command to send them |
| **A Fabric capacity (F SKU or Trial)** | Direct Lake does not work on Power BI Premium (P/PP) SKUs |
| **Python 3.9+** | The tooling. No third-party packages — standard library only |
| **Azure CLI**, signed in | Every API token, and the Entra work |
| **Node.js 18+** | The Rayfin CLI that hosts the front-end |

Check the machine side in one command:

```powershell
git clone <your-fork-url> OneSafe
cd OneSafe
az login --tenant <your-tenant-id>
python tools/setup.py --check
```

`--check` changes nothing. It verifies the tooling, confirms which tenant you are
signed in to, and reports which of the four Fabric tenant settings are already
enabled.

---

## Step 1 — Create the Entra registrations

```powershell
python tools/setup.py --skip-pipeline-run
```

Stop it here if you like, or let it run through; it is safe to re-run at any
point. This creates two app registrations:

- **`OneSafe-Scanner`** — the identity the pipeline runs as. Requests the Graph
  application permissions `User.Read.All`, `Group.Read.All`,
  `Directory.Read.All`, `Application.Read.All`, and gets a client secret.
- **`OneSafe-App`** — a public SPA registration the front-end signs in with. No
  secret; it uses PKCE.

The script prints the consent command. Send it to whoever holds the role:

```powershell
az ad app permission admin-consent --id <scanner-app-id>
```

Nothing works until this is granted. Graph calls will return `403` and the
principal directory will be empty.

> **Do not** add Power BI API permissions to the scanner app in Entra. Power BI
> authorises service principals through the tenant settings in step 2 instead,
> and adding admin-consent-required Power BI permissions actively *breaks* admin
> API access. This is counter-intuitive and costs people a lot of time.

---

## Step 2 — Enable the Fabric tenant settings

**Fabric admin portal → Tenant settings.** Enable all four, each scoped to a
security group that contains the `OneSafe-Scanner` service principal:

| Setting | Without it |
|---|---|
| Service principals can call Fabric public APIs | No API access at all |
| Service principals can access read-only admin APIs | No tenant-wide inventory |
| Enhance admin APIs responses with detailed metadata | Scanner returns items but no per-item user lists |
| Enhance admin APIs responses with DAX and mashup expressions | No semantic-model detail |

The last two matter more than they look. Without them the Scanner API still
returns `200 OK` with almost nothing useful in it — a quiet failure that looks
like *"this tenant has no permissions to report"* rather than a misconfiguration.

Tenant settings take a few minutes to propagate. Re-run
`python tools/setup.py --check` to confirm all four are green before continuing.

---

## Step 3 — Provision Fabric

```powershell
python tools/setup.py --capacity "<your-capacity-name>"
```

This is the main event. It will:

1. Find or create the **OneSafe** workspace and the `lh_onesafe` lakehouse
   (schema-enabled — `bronze` / `silver` / `gold`).
2. Find or create the **OneSafe App** workspace.
3. Write every ID it discovers into `tools/config.json`.
4. Upload the runtime config and the scanner secret into the lakehouse.
5. Deploy the notebooks, build the daily pipeline, and schedule it for 04:30 UTC.
6. **Run the first pipeline** — this takes about 20 minutes and must finish
   before the semantic model can be built, because Direct Lake needs the gold
   tables to physically exist.
7. Build `sm_onesafe` and generate `app/dist/config.js`.

Add `--skip-pipeline-run` to provision everything without the 20-minute wait;
run `python tools/run_pipeline.py` yourself afterwards, then re-run setup to
build the model.

### Choosing a capacity

If you do not pass `--capacity`, the script picks the first *active Fabric*
capacity it can see and tells you what else was available. It refuses to use a
Power BI Premium SKU, and it stops with a clear message if the capacity is
paused — otherwise every later call fails with `Internal error
CapacityNotActive` against an unrelated URL, which reads like a broken script.

You can host the app on a different capacity:

```powershell
python tools/setup.py --capacity "fabric-f64" --app-capacity "fabric-f8"
```

Worth doing if Rayfin refuses to create its item — see [Troubleshooting](#troubleshooting).

---

## Step 4 — Deploy the front-end

```powershell
cd app
npm install
npx rayfin up --workspace-id <appWorkspaceId>
```

`<appWorkspaceId>` is printed by `setup.py` and stored in `tools/config.json`.
Omit `--workspace-id` and Rayfin deploys to *My Workspace*, which is not what
you want. Rayfin prints a hosting URL like
`https://<name>-<region>.webapp.fabricapps.net`.

That origin has to be a registered redirect URI or sign-in fails. Re-run setup
and it reads the URL out of `app/rayfin/.deployments.json` and registers it:

```powershell
cd ..
python tools/setup.py --skip-pipeline-run
```

Redirect URIs are **merged**, never replaced, so this is safe to repeat and
does not disturb any other deployment sharing the `OneSafe-App` registration.

> `rayfin up` writes two deployment-specific values back into
> `app/rayfin/rayfin.yml` — a `publishable_key` and your hosting URL under
> `allowedRedirectUris`. Both are per-deployment, so leave them out of any
> commit you push back to the shared repo.

---

## Step 5 — Verify

```powershell
python tools/query_model.py "EVALUATE {[Principals with Access]}"

node tools/capture_app_dax.js    # extracts every DAX query from the front-end
python tools/check_app_dax.py    # runs them all against the live model
```

The capture step walks each pane of the app under several filter states and
writes out every query it would send; the check step executes them. Together
they are the fastest way to tell a broken deployment from an empty tenant, and
they catch the failure mode where the model built fine but a measure the UI
depends on was renamed.

> PowerShell mangles embedded double quotes before Python ever sees them, so
> anything with a DAX string literal fails to parse. Write the query to a file
> and use `python tools/query_model.py -f query.dax` instead.

Then open the app URL and confirm:

- **Overview** shows non-zero KPIs
- **Principal 360** finds a user you know and lists their workspaces
- **Access Graph** renders nodes when you select a principal

---

## Step 6 — Optional: the demo sandbox

If you want something to look at before your own data is interesting, or you
want to demonstrate OneLake row- and column-level security, create the sandbox:

```powershell
python tools/setup.py --with-demo `
  --demo-user alice@contoso.com `
  --demo-user bob@contoso.com

python tools/run_notebooks.py 97_seed_demo_lakehouse
python tools/seed_onelake_roles.py
python tools/seed_demo_model.py
python tools/run_notebooks.py 98_seed_demo_rls
python tools/run_pipeline.py
```

This creates a separate **OneSafe Demo** workspace with a lakehouse carrying
seven OneLake data access roles (including row filters and column-level
restrictions) and a semantic model with four RLS roles.

Two real users are required, not one: the demo exists to show a role with
several members and a principal that appears in several roles, which a single
user cannot demonstrate. The UPNs must be real signed-in users — one of the
demo RLS roles filters on `USERPRINCIPALNAME()`, and if the UPNs match nobody
the role silently returns no rows and looks broken rather than restrictive.

The sandbox is deliberately a **separate workspace** from `lh_onesafe`. Naming
ordinary users in data access roles on the lakehouse that maps every weak point
in your tenant is the wrong default for a security tool.

To remove it, delete the **OneSafe Demo** workspace in the portal. That drops
the demo lakehouse, its data access roles and the demo semantic model in one
step; then clear the `demo*` keys from `tools/config.json`.

---

## Step 7 — Confirm the schedule

The pipeline is scheduled daily at 04:30 UTC by `build_pipeline.py`. Change
`SCHEDULE_TIME` there and re-run it, or edit the schedule in the Fabric UI.

```powershell
python tools/run_pipeline.py     # run it now
```

---

## Step 8 — Lock it down

**Do this before you tell anyone the URL.**

```powershell
python tools/secure_model.py
```

This creates a security group called `OneSafe Administrators`, records its
object id as `adminGroupId` in `tools/config.json`, grants it Read on
`sm_onesafe` and Viewer on the workspaces, and prints anything it could not
apply so you can finish it in the portal.

What it deliberately does **not** do is decide who belongs in that group. Add
your Fabric admins to it yourself, then remove everyone else's standing access:

- The group should be **Admin** on the OneSafe workspace; nobody else.
- The group should hold **Read + Build** on `sm_onesafe`; nobody else.
- Restrict the app workspace the same way.

The app enforces nothing itself — it queries the semantic model as the signed-in
user, so model permissions *are* the access control. If someone can read
`sm_onesafe`, they can see the whole map.

---

## What `setup.py` will and will not do

| | |
|---|---|
| Creates Entra registrations | Yes |
| Grants admin consent | **No** — no API without a privileged role; the command is printed |
| Toggles Fabric tenant settings | **No** — no write API; they are checked and reported |
| Creates workspaces, lakehouse, notebooks, pipeline, model | Yes |
| Grants the scanner SPN access to its own workspaces | Yes — the Direct Lake refresh reads gold with that identity |
| Deploys the front-end | **No** — run `npx rayfin up` (step 4) |
| Registers the app's hosting URL | Yes, on the next run, once `rayfin up` has produced one |
| Creates the admin security group | Yes, via `tools/secure_model.py` — but it will not decide who belongs in it |
| Deletes anything | **Never** |

Everything is find-or-create. Re-running is safe and is the intended way to
recover from a partial failure — each phase adopts what already exists and
prints whether it created or kept it. Existing capacity assignments, redirect
URIs and client secrets are preserved unless you explicitly ask otherwise.

### Useful flags

| Flag | Effect |
|---|---|
| `--check` | Prerequisites and tenant settings only; changes nothing |
| `--capacity` / `--app-capacity` | Choose capacities by name or id |
| `--workspace-name` | Name the data workspace (default `OneSafe`). The app and demo workspaces derive from it — `<name> App`, `<name> Demo` — unless you override them with `--app-workspace-name` / `--demo-workspace-name` |
| `--skip-pipeline-run` | Provision without the ~20 minute first run |
| `--with-demo`, `--demo-user <upn>` | Also create the demo sandbox |
| `--rotate-secret` | Issue a new scanner secret (invalidates the current one) |
| `--scanner-secret <value>` | Use a secret you already hold |
| `--skip-entra` | Assume the registrations exist in config |

### Running two deployments side by side

`--workspace-name` plus a second clone is enough to stand up an isolated
deployment — a staging copy, or a test of a change before it touches the real
one:

```powershell
git clone <repo> OneSafe-Test
cd OneSafe-Test
python tools/setup.py --workspace-name "OneSafe Test" --scanner-secret <secret>
```

`tools/config.json` is per-clone, so the two deployments cannot overwrite each
other's ids. Reuse the same `OneSafe-Scanner` registration — pass its existing
secret with `--scanner-secret` rather than `--rotate-secret`, which would
invalidate the secret the first deployment is running on. (You can read the
secret back out of the first deployment with
`python tools/read_onelake.py Files/config/onesafe_config.json`.)

The chosen names are remembered in `tools/config.json`, so later re-runs in
that clone do not need `--workspace-name` again — and cannot accidentally
resolve to the default `OneSafe` workspace belonging to the other deployment.

---

## Troubleshooting

**`Internal error CapacityNotActive`**
The capacity is paused. Resume it:

```powershell
az resource invoke-action --action resume `
  --ids /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Fabric/capacities/<name>
```

**Scanner returns almost nothing / the tenant looks empty**
The two *"Enhance admin APIs responses…"* tenant settings are off, or the SPN is
not in the security group they are scoped to. Confirm with
`python tools/setup.py --check`.

**Graph calls return 403, no principals in the model**
Admin consent has not been granted. Step 1.

**Rayfin cannot create its item**
Capacity- and region-dependent. Put the app workspace on a different capacity
with `--app-capacity` and re-run.

**`Expecting value: line 1 column 1` reading config**
`tools/config.json` has a UTF-8 BOM. PowerShell's `Out-File` adds one. Rewrite
it with `[IO.File]::WriteAllText($path, $json, [Text.UTF8Encoding]::new($false))`.

**A notebook job "succeeds" but nothing changed**
Notebook *jobs* produce no cell output, and an unhandled exception cancels the
whole Spark session before later cells run. Use `tools/read_onelake.py` to read
diagnostics the notebook wrote to `Files/`:

```powershell
python tools/read_onelake.py Files/diag                       # list tracebacks
python tools/read_onelake.py Files/diag/error_06_build_gold.txt
```

**`We cannot access the source Delta table '<name>'` during refresh**
The table genuinely does not exist yet, or the identity running the refresh
cannot read the lakehouse. Check, in that order:

1. Did the pipeline reach `06_build_gold`? Look for
   `Files/diag/error_06_build_gold.txt`.
2. Does the scanner SPN hold a role on the data workspace? `setup.py` grants it;
   re-run setup if the workspace was created by hand.
3. Is `sqlEndpointId` set in `tools/config.json`? On a brand-new lakehouse the
   endpoint takes a minute to appear, and a model built before it exists points
   at nothing. Re-run setup to record it and rebuild the model.

**Semantic model refresh fails after a schema change**
Direct Lake tables must exist before the model references them. Run the pipeline
first, then `python tools/build_semantic_model.py`.

---

## Removing OneSafe

Delete the three workspaces in the portal — by default **OneSafe**,
**OneSafe App** and **OneSafe Demo**, or whatever you passed to
`--workspace-name` — then remove the two registrations:

```powershell
az ad app delete --id <scanner-app-id>
az ad app delete --id <spa-app-id>
```

Deleting the workspaces removes the lakehouse and every snapshot in it. There is
no undo, and the snapshot history is the only record of how access changed over
time — export anything you need first.

---

## Where to go next

- [`README.md`](../README.md) — architecture, data model, and the gotchas worth
  knowing before you change anything.
- `tools/config.example.json` — every configuration key, with notes on which are
  discovered and which you supply.

### Repository layout

| Path | What it is |
|---|---|
| `notebooks/` | The PySpark notebooks, in run order. `00_common` holds auth and the paged-GET helper; `01`–`04` extract to bronze, `05` transforms to silver, `06` builds gold, `07`–`10` handle change detection, validation, refresh and failure alerting; `9x_` are demo seeders and diagnostics |
| `tools/` | Host-side Python. `setup.py` bootstraps; the rest deploy, query and seed. Stdlib only |
| `tools/onesafe_config.py` | The config contract. Every tool reads through it, so a missing key gives one actionable message instead of a traceback |
| `app/dist/` | The front-end. Hand-written, **not** build output — there is no build step, Rayfin serves these files verbatim |
| `app/rayfin/` | Rayfin app manifest. `.env` and `.deployments.json` are gitignored |
| `docs/` | This guide |

Files that describe *your* deployment — `tools/config.json`,
`tools/notebook_ids.json`, `app/dist/config.js`, `app/rayfin/.env` — are
gitignored. They are generated, not authored; never commit them.
