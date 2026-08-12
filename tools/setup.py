"""OneSafe one-command bootstrap.

Provisions a complete OneSafe deployment into a Fabric tenant: Entra app
registrations, workspaces, lakehouse, notebooks, pipeline, semantic model and
optionally the demo sandbox.

    python tools/setup.py --check          # prerequisites only, changes nothing
    python tools/setup.py                  # provision everything
    python tools/setup.py --with-demo      # ...including the RLS/CLS sandbox
    python tools/setup.py --resume         # re-run after fixing something

Design notes
------------
**Everything is find-or-create.** Re-running is safe and is the intended way to
recover from a partial failure: each phase checks for what it would create and
adopts it if present. Nothing is ever deleted.

**State lives in tools/config.json, not in this script.** Each phase writes what
it learned immediately, so a crash in phase 6 does not throw away phases 1-5.
That file is gitignored — it describes one deployment in one tenant.

**Steps with no API are reported, not faked.** Fabric tenant settings and Entra
admin consent cannot be automated. Rather than failing obscurely three phases
later, setup checks whether they have been done and prints exactly what to click.
A bootstrap that pretends to have finished when it has not is worse than one
that stops.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from onesafe_config import CONFIG_PATH, REPO_ROOT, TOOLS_DIR, load, save  # noqa: E402

FABRIC_API = "https://api.fabric.microsoft.com"
GRAPH_API = "https://graph.microsoft.com"

SCANNER_APP_NAME = "OneSafe-Scanner"
SPA_APP_NAME = "OneSafe-App"
DATA_WORKSPACE = "OneSafe"
APP_WORKSPACE = "OneSafe App"
DEMO_WORKSPACE = "OneSafe Demo"
LAKEHOUSE_NAME = "lh_onesafe"
DEMO_LAKEHOUSE_NAME = "lh_onesafe_demo"

# Application permissions the scanner needs on Graph. Power BI rights are NOT
# granted here on purpose — see check_tenant_settings().
GRAPH_ROLES = ["User.Read.All", "Group.Read.All", "Directory.Read.All",
               "Application.Read.All"]
GRAPH_RESOURCE_APP_ID = "00000003-0000-0000-c000-000000000000"


# ------------------------------------------------------------------ presentation

class Style:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    OFF = "\033[0m"


_step = 0


def phase(title: str) -> None:
    global _step
    _step += 1
    print(f"\n{Style.BOLD}{Style.CYAN}[{_step}] {title}{Style.OFF}")


def ok(msg: str) -> None:
    print(f"    {Style.GREEN}v{Style.OFF} {msg}")


def kept(msg: str) -> None:
    print(f"    {Style.DIM}= {msg}{Style.OFF}")


def warn(msg: str) -> None:
    print(f"    {Style.YELLOW}!{Style.OFF} {msg}")


def fail(msg: str) -> None:
    print(f"    {Style.RED}x{Style.OFF} {msg}")


def action(msg: str) -> None:
    print(f"    {Style.YELLOW}->{Style.OFF} {msg}")


# ------------------------------------------------------------------- transport

def sh(args: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, shell=True, check=check)


def token(resource: str = FABRIC_API) -> str:
    try:
        return sh(["az", "account", "get-access-token", "--resource", resource,
                   "--query", "accessToken", "-o", "tsv"]).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"\nCould not get a token for {resource}.\n"
            f"  Run `az login --tenant <your-tenant-id>` first.\n"
            f"  {(exc.stderr or '').strip()[:300]}\n"
        ) from None


def api(method: str, url: str, tok: str, body: Any = None,
        tolerate: tuple = ()) -> tuple[int, Any]:
    """One HTTP call. Returns (status, parsed-body).

    `tolerate` lists status codes that are expected and should be returned
    rather than raised — used for the find half of find-or-create.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:600]
        if exc.code in tolerate:
            return exc.code, detail
        raise RuntimeError(f"{exc.code} {method} {url}\n      {detail}") from None


def poll_lro(status: int, headers_location: Optional[str], tok: str,
             timeout_s: int = 300) -> None:
    """Fabric create/update is long-running: 202 + Location, poll to terminal."""
    if status != 202 or not headers_location:
        return
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        _, state = api("GET", headers_location, tok)
        if (state or {}).get("status") in ("Succeeded", "Completed"):
            return
        if (state or {}).get("status") == "Failed":
            raise RuntimeError(f"operation failed: {json.dumps(state)[:400]}")
    raise RuntimeError("operation timed out")


# --------------------------------------------------------------- prerequisites

def check_prerequisites() -> bool:
    phase("Checking prerequisites")
    good = True

    if sys.version_info < (3, 9):
        fail(f"Python 3.9+ required, found {sys.version.split()[0]}")
        good = False
    else:
        ok(f"Python {sys.version.split()[0]}")

    for tool, probe, why in (
        ("az", ["az", "version"], "Azure CLI — used for every token and the Entra work"),
        ("node", ["node", "--version"], "Node.js — runs the Rayfin CLI and the front-end tests"),
        ("npx", ["npx", "--version"], "npx — deploys the app"),
    ):
        try:
            sh(probe)
            ok(f"{tool} present")
        except Exception:
            fail(f"{tool} not found on PATH — {why}")
            good = False

    try:
        acct = json.loads(sh(["az", "account", "show", "-o", "json"]).stdout)
        ok(f"signed in as {acct.get('user', {}).get('name')} "
           f"(tenant {acct.get('tenantId')})")
    except Exception:
        fail("not signed in — run `az login --tenant <your-tenant-id>`")
        good = False

    return good


def check_tenant_settings(tok: str) -> None:
    """Verify the four Fabric tenant settings the scanner depends on.

    These have no write API and are the single most common reason a fresh
    OneSafe deployment produces an empty-looking tenant: without the last two,
    the Scanner API returns 200 with almost no useful content — a quiet failure
    that looks like "this tenant has no permissions to report".
    """
    phase("Checking Fabric tenant settings")

    required = {
        "ServicePrincipalAccessPermissionAPIs": "Service principals can call Fabric public APIs",
        "AllowServicePrincipalsUseReadAdminAPIs": "Service principals can access read-only admin APIs",
        "AdminApisIncludeDetailedMetadata": "Enhance admin APIs responses with detailed metadata",
        "AdminApisIncludeExpressions": "Enhance admin APIs responses with DAX and mashup expressions",
    }

    try:
        _, body = api("GET", f"{FABRIC_API}/v1/admin/tenantsettings", tok)
        settings = {s.get("settingName"): s for s in (body.get("tenantSettings") or [])}
    except Exception as exc:
        warn(f"could not read tenant settings ({str(exc)[:120]})")
        action("Verify them by hand - see docs/SETUP.md step 2")
        return

    missing = []
    for name, label in required.items():
        s = settings.get(name)
        if s is None:
            warn(f"'{label}' not reported by the API — check by hand")
        elif s.get("enabled"):
            ok(label)
        else:
            fail(label)
            missing.append(label)

    if missing:
        action("Enable these in Fabric admin portal > Tenant settings, scoped to a")
        action("security group containing the OneSafe-Scanner service principal:")
        for m in missing:
            print(f"         - {m}")


# -------------------------------------------------------------------- entra

def ensure_scanner_app(cfg: Dict[str, Any], no_secret: bool,
                       rotate: bool = False) -> Optional[str]:
    """Find-or-create the scanner app registration. Returns a secret if issued."""
    phase(f"Entra app registration '{SCANNER_APP_NAME}'")

    found = json.loads(sh([
        "az", "ad", "app", "list", "--display-name", SCANNER_APP_NAME,
        "--query", "[].{appId:appId,id:id}", "-o", "json",
    ]).stdout or "[]")

    if found:
        app_id = found[0]["appId"]
        existed = True
        kept(f"already exists: {app_id}")
    else:
        created = json.loads(sh([
            "az", "ad", "app", "create", "--display-name", SCANNER_APP_NAME,
            "--sign-in-audience", "AzureADMyOrg", "-o", "json",
        ]).stdout)
        app_id = created["appId"]
        existed = False
        ok(f"created: {app_id}")

    sp = json.loads(sh([
        "az", "ad", "sp", "list", "--filter", f"appId eq '{app_id}'",
        "--query", "[].id", "-o", "json",
    ]).stdout or "[]")
    if sp:
        sp_object_id = sp[0]
        kept(f"service principal: {sp_object_id}")
    else:
        sp_object_id = json.loads(sh([
            "az", "ad", "sp", "create", "--id", app_id, "-o", "json",
        ]).stdout)["id"]
        ok(f"service principal created: {sp_object_id}")

    for role in GRAPH_ROLES:
        try:
            role_id = json.loads(sh([
                "az", "ad", "sp", "show", "--id", GRAPH_RESOURCE_APP_ID,
                "--query", f"appRoles[?value=='{role}'].id", "-o", "json",
            ]).stdout)[0]
            sh(["az", "ad", "app", "permission", "add", "--id", app_id,
                "--api", GRAPH_RESOURCE_APP_ID, "--api-permissions",
                f"{role_id}=Role"], check=False)
        except Exception:
            warn(f"could not request {role} — add it by hand")
    ok(f"requested Graph application permissions: {', '.join(GRAPH_ROLES)}")

    cfg["scannerAppId"] = app_id
    cfg["scannerSpObjectId"] = sp_object_id
    save(cfg)

    # A secret is only ever issued for an app this run created. `az ad app
    # credential reset` does what it says: it *replaces* the credential set, so
    # running setup again on an existing deployment would invalidate the secret
    # the running pipeline authenticates with. Rotation has to be asked for.
    secret = None
    if no_secret:
        kept("no secret requested")
    elif existed and not rotate:
        kept("existing app kept its current secret (pass --rotate-secret to replace it)")
    else:
        try:
            secret = json.loads(sh([
                "az", "ad", "app", "credential", "reset", "--id", app_id,
                "--display-name", "onesafe-setup", "--years", "1", "-o", "json",
            ]).stdout)["password"]
            ok("client secret issued (shown once, at the end)")
            if existed:
                warn("previous secrets for this app are now invalid")
        except Exception as exc:
            warn(f"could not issue a secret ({str(exc)[:120]})")

    action("Grant admin consent (needs a Global/Privileged Role Administrator):")
    print(f"         az ad app permission admin-consent --id {app_id}")
    return secret


def ensure_spa_app(cfg: Dict[str, Any], redirect_uris: List[str]) -> None:
    """Find-or-create the public SPA registration the front-end signs in with."""
    phase(f"Entra app registration '{SPA_APP_NAME}'")

    found = json.loads(sh([
        "az", "ad", "app", "list", "--display-name", SPA_APP_NAME,
        "--query", "[].{appId:appId,id:id}", "-o", "json",
    ]).stdout or "[]")

    if found:
        app_id, obj_id = found[0]["appId"], found[0]["id"]
        kept(f"already exists: {app_id}")
    else:
        created = json.loads(sh([
            "az", "ad", "app", "create", "--display-name", SPA_APP_NAME,
            "--sign-in-audience", "AzureADMyOrg", "-o", "json",
        ]).stdout)
        app_id, obj_id = created["appId"], created["id"]
        ok(f"created: {app_id}")

    # SPA redirect URIs must sit under the `spa` platform, not `web`: only the
    # spa platform enables PKCE without a client secret, which is the whole
    # point of a public client. Existing URIs are merged, never replaced — the
    # hosted Rayfin origin is added after the first deploy and must survive
    # every later re-run of setup.
    tok = token(GRAPH_API)
    existing: List[str] = []
    try:
        _, app = api("GET", f"{GRAPH_API}/v1.0/applications/{obj_id}?$select=spa", tok)
        existing = ((app or {}).get("spa") or {}).get("redirectUris") or []
    except Exception:
        pass

    uris = sorted(set(existing) | set(redirect_uris) | {"http://localhost:5173/"})
    if set(uris) == set(existing):
        kept(f"redirect URIs unchanged ({len(uris)})")
    else:
        try:
            api("PATCH", f"{GRAPH_API}/v1.0/applications/{obj_id}", tok,
                {"spa": {"redirectUris": uris}})
            ok(f"redirect URIs: {', '.join(uris)}")
        except Exception as exc:
            warn(f"could not set redirect URIs ({str(exc)[:160]})")
            action(f"Add these by hand under Authentication > SPA: {', '.join(uris)}")

    cfg["spaAppId"] = app_id
    cfg["spaObjectId"] = obj_id
    save(cfg)


# ------------------------------------------------------------------- fabric

def pick_capacity(tok: str, cfg: Dict[str, Any], preferred: Optional[str]) -> str:
    """Choose the capacity to host the workspaces.

    Direct Lake needs a *Fabric* capacity (F SKU or Trial). Power BI Premium
    SKUs (P/PP/A/EM) show up on this endpoint too and must not be chosen: the
    workspaces would be created but the semantic model would silently fall back
    off Direct Lake.

    A paused capacity reports state "Inactive". That is worth failing on early,
    because every later call returns "Internal error CapacityNotActive" against
    an unrelated URL, which reads like a broken script rather than a paused SKU.
    """
    phase("Resolving Fabric capacity")
    _, body = api("GET", f"{FABRIC_API}/v1/capacities", tok)
    caps = list(body.get("value") or [])
    if not caps:
        raise SystemExit("\nNo Fabric capacity visible to you. Assign or start one first.\n")

    def by_name_or_id(value: str) -> Optional[Dict[str, Any]]:
        return next((c for c in caps if c["id"].lower() == value.lower()
                     or (c.get("displayName") or "").lower() == value.lower()), None)

    def is_fabric(c: Dict[str, Any]) -> bool:
        sku = (c.get("sku") or "").upper()
        return sku.startswith("F") or sku.startswith("TRIAL")

    def describe(c: Dict[str, Any]) -> str:
        return (f"{c.get('displayName')}  {c['id']}  "
                f"{c.get('sku')}  {c.get('region')}  {c.get('state')}")

    def check_active(c: Optional[Dict[str, Any]], cap_id: str) -> None:
        if c and c.get("state") and c["state"] != "Active":
            fail(f"capacity is {c['state']} — resume it before continuing")
            action("az resource invoke-action --action resume \\")
            action(f"  --ids /subscriptions/<sub>/resourceGroups/<rg>/providers/"
                   f"Microsoft.Fabric/capacities/{c.get('displayName')}")
            raise SystemExit(
                f"\nCapacity {cap_id} is paused. Every Fabric API call would fail "
                "with\n'Internal error CapacityNotActive'. Resume it and re-run.\n")

    # An existing deployment keeps its capacity unless explicitly moved.
    # Re-picking on every run would reassign live workspaces, which is exactly
    # what a resumable setup script must never do.
    if not preferred and cfg.get("capacityId"):
        current = by_name_or_id(cfg["capacityId"])
        kept(describe(current) if current else f"{cfg['capacityId']} (from existing config)")
        check_active(current, cfg["capacityId"])
        return cfg["capacityId"]

    if preferred:
        chosen = by_name_or_id(preferred)
        if not chosen:
            raise SystemExit(f"\nCapacity '{preferred}' not found. Available:\n  " +
                             "\n  ".join(describe(c) for c in caps) + "\n")
        if not is_fabric(chosen):
            warn(f"'{chosen.get('displayName')}' is SKU {chosen.get('sku')}, not a "
                 "Fabric capacity — Direct Lake will not work")
        check_active(chosen, chosen["id"])
    else:
        fabric_caps = [c for c in caps if is_fabric(c)]
        if not fabric_caps:
            raise SystemExit(
                "\nNo Fabric (F SKU or Trial) capacity found. OneSafe needs one for\n"
                "Direct Lake. Capacities visible to you:\n  "
                + "\n  ".join(describe(c) for c in caps) + "\n")
        active = [c for c in fabric_caps if c.get("state") == "Active"]
        chosen = (active or fabric_caps)[0]
        if len(fabric_caps) > 1:
            warn(f"{len(fabric_caps)} Fabric capacities found; using "
                 f"'{chosen.get('displayName')}'. Pass --capacity to choose another:")
            for c in fabric_caps:
                print(f"         {describe(c)}")
        check_active(chosen, chosen["id"])

    ok(describe(chosen))
    cfg["capacityId"] = chosen["id"]
    save(cfg)
    return chosen["id"]


def ensure_workspace(tok: str, name: str, capacity_id: str, description: str) -> str:
    _, body = api("GET", f"{FABRIC_API}/v1/workspaces", tok)
    ws = next((w for w in (body.get("value") or []) if w.get("displayName") == name), None)
    created = False

    if ws:
        kept(f"workspace '{name}': {ws['id']}")
    else:
        _, ws = api("POST", f"{FABRIC_API}/v1/workspaces", tok,
                    {"displayName": name, "description": description})
        ok(f"workspace '{name}' created: {ws['id']}")
        created = True
        time.sleep(5)

    current = (ws.get("capacityId") or "")
    if current.lower() == capacity_id.lower():
        return ws["id"]

    if current and not created:
        # A workspace that already sits on a capacity is left alone. Moving it
        # would detach its Direct Lake model and, for the app workspace, can
        # land it on a capacity where Rayfin cannot create items.
        warn(f"'{name}' is on capacity {current}, not {capacity_id} — left as is")
        action(f"To move it deliberately: --capacity {capacity_id}")
        return ws["id"]

    api("POST", f"{FABRIC_API}/v1/workspaces/{ws['id']}/assignToCapacity", tok,
        {"capacityId": capacity_id})
    ok(f"assigned '{name}' to capacity")
    return ws["id"]


def ensure_lakehouse(tok: str, workspace_id: str, name: str,
                     schema_enabled: bool = True) -> Dict[str, str]:
    _, body = api("GET", f"{FABRIC_API}/v1/workspaces/{workspace_id}/lakehouses", tok)
    lh = next((i for i in (body.get("value") or []) if i.get("displayName") == name), None)

    if lh:
        kept(f"lakehouse '{name}': {lh['id']}")
    else:
        payload: Dict[str, Any] = {"displayName": name}
        if schema_enabled:
            # Schema-enabled is required: the medallion layers are bronze/silver/
            # gold *schemas*, and this cannot be changed after creation.
            payload["creationPayload"] = {"enableSchemas": True}
        st, created = api("POST", f"{FABRIC_API}/v1/workspaces/{workspace_id}/lakehouses",
                          tok, payload)
        lh = created if isinstance(created, dict) and created.get("id") else None
        for _ in range(30):
            time.sleep(5)
            _, body = api("GET", f"{FABRIC_API}/v1/workspaces/{workspace_id}/lakehouses", tok)
            lh = next((i for i in (body.get("value") or [])
                       if i.get("displayName") == name), None)
            if lh:
                break
        if not lh:
            raise RuntimeError(f"lakehouse '{name}' did not appear after creation")
        ok(f"lakehouse '{name}' created: {lh['id']}")

    props = lh.get("properties") or {}
    sql_ep = props.get("sqlEndpointProperties") or {}
    return {
        "id": lh["id"],
        "sqlEndpointId": sql_ep.get("id", ""),
        "sqlEndpoint": sql_ep.get("connectionString", ""),
    }


def run_tool(script: str, *args: str, optional: bool = False) -> bool:
    """Run one of the other tools in-process order, surfacing its output."""
    cmd = [sys.executable, str(TOOLS_DIR / script), *args]
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), text=True,
                          capture_output=True, shell=False)
    tail = (proc.stdout or "").strip().splitlines()
    for line in tail[-6:]:
        print(f"      {Style.DIM}{line}{Style.OFF}")
    if proc.returncode != 0:
        (warn if optional else fail)(f"{script} exited {proc.returncode}")
        for line in (proc.stderr or "").strip().splitlines()[-8:]:
            print(f"      {Style.DIM}{line}{Style.OFF}")
        return False
    ok(f"{script} completed")
    return True


def write_app_config(cfg: Dict[str, Any]) -> None:
    """Generate app/dist/config.js from the provisioned IDs."""
    target = REPO_ROOT / "app" / "dist" / "config.js"
    target.write_text(
        "// OneSafe runtime configuration - GENERATED by tools/setup.py.\n"
        "// Edit tools/config.json and re-run setup rather than editing this file.\n"
        "window.CONFIG = {\n"
        f'  tenantId: "{cfg.get("tenantId", "")}",\n'
        f'  clientId: "{cfg.get("spaAppId", "")}",\n'
        f'  datasetId: "{cfg.get("semanticModelId", "")}",\n'
        f'  workspaceId: "{cfg.get("workspaceId", "")}",\n'
        '  pbiScopes: ["https://analysis.windows.net/powerbi/api/Dataset.Read.All"],\n'
        '  fabricBase: "https://app.fabric.microsoft.com",\n'
        "};\n",
        encoding="utf-8",
    )
    ok("wrote app/dist/config.js")


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Provision a complete OneSafe deployment into a Fabric tenant.")
    ap.add_argument("--check", action="store_true",
                    help="verify prerequisites and stop, changing nothing")
    ap.add_argument("--capacity",
                    help="capacity name or id to host the workspaces")
    ap.add_argument("--app-capacity",
                    help="capacity for the app workspace (Rayfin is not available "
                         "on every capacity/region; defaults to --capacity)")
    ap.add_argument("--with-demo", action="store_true",
                    help="also create the RLS/CLS demo sandbox")
    ap.add_argument("--demo-user", action="append", default=[],
                    help="UPN to assign demo roles to; pass twice (implies --with-demo)")
    ap.add_argument("--scanner-secret",
                    help="use an existing scanner client secret instead of issuing one")
    ap.add_argument("--no-secret", action="store_true",
                    help="do not issue a client secret (you will supply one later)")
    ap.add_argument("--rotate-secret", action="store_true",
                    help="issue a new scanner secret even if the app already exists "
                         "(this invalidates the existing one)")
    ap.add_argument("--skip-entra", action="store_true",
                    help="assume the app registrations already exist in config")
    ap.add_argument("--skip-pipeline-run", action="store_true",
                    help="provision everything but do not run the first pipeline")
    args = ap.parse_args()

    print(f"{Style.BOLD}OneSafe setup{Style.OFF}")

    if not check_prerequisites():
        print(f"\n{Style.RED}Prerequisites missing. Fix the above and re-run.{Style.OFF}\n")
        return 1
    if args.check:
        tok = token(FABRIC_API)
        check_tenant_settings(tok)
        print(f"\n{Style.GREEN}Prerequisite check complete. "
              f"Run without --check to provision.{Style.OFF}\n")
        return 0

    cfg = load() if CONFIG_PATH.exists() else {}
    cfg["tenantId"] = json.loads(sh(["az", "account", "show", "-o", "json"]).stdout)["tenantId"]
    save(cfg)

    secret = args.scanner_secret
    if not args.skip_entra:
        issued = ensure_scanner_app(cfg, no_secret=args.no_secret or bool(secret),
                                    rotate=args.rotate_secret)
        secret = secret or issued

    tok_fab = token(FABRIC_API)
    capacity_id = pick_capacity(tok_fab, cfg, args.capacity)

    phase("Fabric workspaces and lakehouse")
    cfg["workspaceName"] = DATA_WORKSPACE
    cfg["workspaceId"] = ensure_workspace(
        tok_fab, DATA_WORKSPACE, capacity_id,
        "OneSafe security 360 - data plane. Holds a map of every access path in "
        "the tenant; keep membership restricted to Fabric admins.")
    lh = ensure_lakehouse(tok_fab, cfg["workspaceId"], LAKEHOUSE_NAME)
    cfg["lakehouseName"] = LAKEHOUSE_NAME
    cfg["lakehouseId"] = lh["id"]
    if lh["sqlEndpointId"]:
        cfg["sqlEndpointId"] = lh["sqlEndpointId"]
        cfg["sqlEndpoint"] = lh["sqlEndpoint"]
        ok(f"SQL endpoint {lh['sqlEndpointId']}")
    else:
        warn("SQL endpoint not provisioned yet; re-run setup in a minute to record it")
    save(cfg)

    # The app lives apart from the data on purpose: Rayfin item creation is not
    # available on every capacity, and the front-end should not carry rights
    # over the lakehouse that produced the security map. An already-chosen app
    # capacity is preserved — if Rayfin worked there, moving it would break it.
    app_capacity = args.app_capacity or cfg.get("appCapacityId") or capacity_id
    cfg["appWorkspaceId"] = ensure_workspace(
        tok_fab, APP_WORKSPACE, app_capacity,
        "Front-end host for the OneSafe security 360 app.")
    cfg["appCapacityId"] = app_capacity
    save(cfg)

    demo_users = args.demo_user
    if args.with_demo or demo_users:
        phase("Demo sandbox")
        cfg["demoWorkspaceName"] = DEMO_WORKSPACE
        cfg["demoWorkspaceId"] = ensure_workspace(
            tok_fab, DEMO_WORKSPACE, capacity_id,
            "OneSafe demo sandbox - RLS/CLS examples. Contains no real data.")
        dlh = ensure_lakehouse(tok_fab, cfg["demoWorkspaceId"], DEMO_LAKEHOUSE_NAME)
        cfg["demoLakehouseName"] = DEMO_LAKEHOUSE_NAME
        cfg["demoLakehouseId"] = dlh["id"]

        resolved = []
        tok_graph = token(GRAPH_API)
        for upn in demo_users:
            try:
                _, u = api("GET",
                           f"{GRAPH_API}/v1.0/users/{urllib.parse.quote(upn)}"
                           "?$select=id,userPrincipalName", tok_graph)
                resolved.append({"objectId": u["id"], "upn": u["userPrincipalName"]})
                ok(f"demo principal {u['userPrincipalName']}")
            except Exception as exc:
                fail(f"could not resolve '{upn}': {str(exc)[:160]}")
        if resolved:
            cfg["demoPrincipals"] = resolved
        elif not cfg.get("demoPrincipals"):
            warn("no demo principals resolved — pass --demo-user <upn> twice")
            action("The demo seeders need two real users to show a multi-member role")
        save(cfg)

    if not args.skip_entra:
        # The app is served from the Rayfin hosting URL, which does not exist
        # until the first deploy. localhost is registered now so the app can be
        # run locally; setup prints the follow-up for the hosted origin.
        ensure_spa_app(cfg, redirect_uris=[])

    phase("Uploading runtime config to OneLake")
    if secret:
        run_tool("upload_config.py", secret)
    elif CONFIG_PATH.exists():
        if not run_tool("upload_config.py", "--sync", optional=True):
            warn("no stored secret found — supply one:")
            action("python tools/upload_config.py <client-secret>")

    phase("Deploying notebooks")
    run_tool("deploy_notebooks.py")

    phase("Building the daily pipeline")
    run_tool("build_pipeline.py")
    cfg = load()

    check_tenant_settings(token(FABRIC_API))

    if not args.skip_pipeline_run:
        phase("Running the first pipeline (about 20 minutes)")
        print(f"      {Style.DIM}This must finish before the semantic model can be "
              f"built — Direct Lake needs the gold tables to exist.{Style.OFF}")
        run_tool("run_pipeline.py")

    phase("Building the semantic model")
    if run_tool("build_semantic_model.py"):
        cfg = load()
        run_tool("upload_config.py", "--sync", optional=True)

    write_app_config(load())

    # ----------------------------------------------------------- what is left
    cfg = load()
    print(f"\n{Style.BOLD}Provisioning complete.{Style.OFF}\n")
    print(f"  data workspace   {cfg.get('workspaceId')}")
    print(f"  lakehouse        {cfg.get('lakehouseId')}")
    print(f"  semantic model   {cfg.get('semanticModelId')}")
    print(f"  app workspace    {cfg.get('appWorkspaceId')}")

    if secret:
        print(f"\n{Style.YELLOW}Scanner client secret (shown once — it is already "
              f"stored in OneLake):{Style.OFF}\n  {secret}")

    print(f"\n{Style.BOLD}Remaining manual steps{Style.OFF}")
    print("  These have no API. OneSafe will run without them but will under-report.\n")
    print(f"  1. Grant Entra admin consent:")
    print(f"       az ad app permission admin-consent --id {cfg.get('scannerAppId')}")
    print(f"  2. Enable the Fabric tenant settings listed above, scoped to a group")
    print(f"     containing the scanner SPN ({cfg.get('scannerSpObjectId')}).")
    print(f"  3. Deploy the app:")
    print(f"       cd app && npx rayfin up")
    print(f"  4. Add the printed hosting URL as a redirect URI on {SPA_APP_NAME}:")
    print(f"       az ad app update --id {cfg.get('spaAppId')} \\")
    print(f"         --set spa.redirectUris=\"['https://<your-app>.webapp.fabricapps.net/']\"")
    if cfg.get("demoWorkspaceId"):
        print(f"  5. Seed the demo sandbox:")
        print(f"       python tools/run_notebooks.py 97_seed_demo_lakehouse")
        print(f"       python tools/seed_onelake_roles.py")
        print(f"       python tools/seed_demo_model.py")
        print(f"       python tools/run_notebooks.py 98_seed_demo_rls")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
