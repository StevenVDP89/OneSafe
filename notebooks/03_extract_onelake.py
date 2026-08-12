# CELL ********************

# OneSafe :: 03_extract_onelake
# Deep security extraction - the two layers the workspace/item APIs cannot see:
#
#   1. OneLake Security     data access roles on Lakehouses and friends,
#                           including their row and column constraints.
#   2. Semantic model RLS   row-level filter expressions and column-level
#      and CLS/OLS          (object-level) permissions, read from the model
#                           definition (TMSL), which is the only place the
#                           service exposes the actual rules.
#
# The scanner API reports RLS role *names and members* but not what those roles
# actually restrict, and it reports nothing at all about column-level security.
# Without the rules, "Ivana is in role DemoNoPII" is unactionable - an admin
# cannot tell whether that role hides a PII column or does nothing.

# CELL ********************

# MAGIC %run 00_common

# CELL ********************

from concurrent.futures import ThreadPoolExecutor, as_completed

# Item types that can carry OneLake data access roles.
ONELAKE_ITEM_TYPES = {"Lakehouse", "Warehouse", "SQLDatabase", "MirroredDatabase"}

scan_workspaces = read_bronze("scan_workspaces")
if not scan_workspaces:
    raise RuntimeError("No scan results in bronze - run 02_extract_scanner first.")

# CELL ********************

# The scanner returns Fabric item types as PascalCase keys and legacy Power BI
# collections as lowercase plurals. Pick out only the OneLake-capable ones.
targets = []
for ws in scan_workspaces:
    ws_id = ws.get("id")
    for key in ONELAKE_ITEM_TYPES:
        for item in ws.get(key) or []:
            if item.get("id"):
                targets.append(
                    {
                        "workspaceId": ws_id,
                        "workspaceName": ws.get("name"),
                        "itemId": item["id"],
                        "itemName": item.get("name"),
                        "itemType": key,
                    }
                )

print(f"[onesafe] {len(targets)} OneLake-capable items to inspect")

# CELL ********************

def fetch_roles(target):
    url = (
        f"{FABRIC_API}/v1/workspaces/{target['workspaceId']}"
        f"/items/{target['itemId']}/dataAccessRoles"
    )
    rec = dict(target)
    rec["roles"] = []
    rec["accessDenied"] = False
    rec["coverageStatus"] = "Ok"

    params = {}
    seen_tokens = set()
    while True:
        try:
            page = api_get(url, RES_FABRIC, params=params)
        except ApiError as exc:
            body = (exc.body or "")
            # An empty role list and a lakehouse that *cannot* hold roles are very
            # different security statements. Reporting both as "0 roles" is what
            # made the OneLake pane read as though the feature was simply unused.
            if "UniversalSecurityFeatureDisabled" in body:
                rec["coverageStatus"] = "FeatureDisabled"
            elif exc.status in (401, 403):
                rec["coverageStatus"] = "AccessDenied"
                rec["accessDenied"] = True
            elif exc.status in (400, 404):
                rec["coverageStatus"] = "NotSupported"
            else:
                rec["coverageStatus"] = "Error"
                rec["accessDenied"] = True
            rec["error"] = str(exc)[:400]
            break

        if page is None:
            break
        rec["roles"].extend(page.get("value") or [])

        token = page.get("continuationToken")
        if not token or token in seen_tokens:
            break
        seen_tokens.add(token)
        params["continuationToken"] = token

    return rec


results = []
with ThreadPoolExecutor(max_workers=8) as pool:
    futures = [pool.submit(fetch_roles, t) for t in targets]
    for i, fut in enumerate(as_completed(futures), 1):
        results.append(fut.result())
        if i % 50 == 0:
            print(f"[onesafe] onelake roles {i}/{len(targets)}")

# CELL ********************

write_bronze("onelake_roles", results)

denied = sum(1 for r in results if r.get("accessDenied"))
with_roles = sum(1 for r in results if r.get("roles"))
disabled = sum(1 for r in results if r.get("coverageStatus") == "FeatureDisabled")

# CELL ********************

# --------------------------------------------------------- semantic model RLS/CLS
#
# The model definition (TMSL) is the only surface that returns the actual rules:
#   model.roles[].tablePermissions[].filterExpression   -> row-level security
#   model.roles[].tablePermissions[].columnPermissions  -> column-level security
#
# getDefinition is a long-running operation: 202 + Location, poll to terminal,
# then GET {Location}/result. api_request() only hands back parsed bodies, so
# this uses requests directly to see the Location header.

import base64

model_targets = []
for ws in scan_workspaces:
    ws_id = ws.get("id")
    # Scanner returns legacy Power BI collections lowercased.
    for item in (ws.get("datasets") or []) + (ws.get("SemanticModel") or []):
        if item.get("id"):
            model_targets.append(
                {
                    "workspaceId": ws_id,
                    "workspaceName": ws.get("name"),
                    "itemId": item["id"],
                    "itemName": item.get("name"),
                    "itemType": "SemanticModel",
                }
            )

print(f"[onesafe] {len(model_targets)} semantic models to inspect for RLS/CLS")

# CELL ********************

MODEL_DEF_POLL_SECONDS = 3
MODEL_DEF_MAX_POLLS = 40


def _raw(method, url, **kwargs):
    return requests.request(method, url, headers=auth_header(RES_FABRIC), timeout=300, **kwargs)


def fetch_model_definition(target):
    """Return the parsed TMSL for one semantic model, or a coverage status."""
    rec = dict(target)
    rec["roles"] = []
    rec["coverageStatus"] = "Ok"
    ws_id, item_id = target["workspaceId"], target["itemId"]
    url = f"{FABRIC_API}/v1/workspaces/{ws_id}/semanticModels/{item_id}/getDefinition?format=TMSL"

    try:
        resp = _raw("POST", url, json={})
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                rec["coverageStatus"] = "Error"
                rec["error"] = "202 without Location header"
                return rec
            for _ in range(MODEL_DEF_MAX_POLLS):
                time.sleep(MODEL_DEF_POLL_SECONDS)
                poll = _raw("GET", location)
                if poll.status_code >= 400:
                    break
                state = (poll.json() or {}).get("status")
                if state == "Succeeded":
                    resp = _raw("GET", location.rstrip("/") + "/result")
                    break
                if state == "Failed":
                    rec["coverageStatus"] = "Error"
                    rec["error"] = str(poll.json())[:400]
                    return rec
            else:
                rec["coverageStatus"] = "Error"
                rec["error"] = "definition operation timed out"
                return rec

        if resp.status_code in (401, 403):
            # Same rule as OneLake: an unreadable model is *unknown*, never
            # "has no row-level security".
            rec["coverageStatus"] = "AccessDenied"
            rec["error"] = resp.text[:400]
            return rec
        if resp.status_code >= 400:
            body = resp.text or ""
            # Direct Lake / push / streaming models have no TMSL definition to read.
            rec["coverageStatus"] = "NotSupported" if resp.status_code in (400, 404) else "Error"
            rec["error"] = body[:400]
            return rec

        parts = (resp.json() or {}).get("definition", {}).get("parts", [])
    except Exception as exc:  # network, JSON, anything
        rec["coverageStatus"] = "Error"
        rec["error"] = str(exc)[:400]
        return rec

    for part in parts:
        if not str(part.get("path", "")).endswith(".bim"):
            continue
        try:
            tmsl = json.loads(base64.b64decode(part["payload"]).decode("utf-8", "replace"))
        except Exception as exc:
            rec["coverageStatus"] = "Error"
            rec["error"] = f"unparsable model.bim: {exc}"[:400]
            return rec
        model = tmsl.get("model") or {}
        rec["roles"] = model.get("roles") or []
        rec["tableCount"] = len(model.get("tables") or [])
        break

    return rec


model_results = []
# Definition reads are long-running per model; keep concurrency modest so a
# large tenant does not trip the Fabric operation throttle.
with ThreadPoolExecutor(max_workers=6) as pool:
    futures = [pool.submit(fetch_model_definition, t) for t in model_targets]
    for i, fut in enumerate(as_completed(futures), 1):
        model_results.append(fut.result())
        if i % 25 == 0:
            print(f"[onesafe] model definitions {i}/{len(model_targets)}")

# CELL ********************

# ------------------------------------------------------- RLS role *membership*
#
# Neither of the two obvious sources works:
#   * getDefinition strips roles[].members[] on read as well as on write;
#   * the Scanner API's rowLevelSecurity[] is simply never populated — verified
#     across a full tenant scan, zero occurrences, while TOM showed the members
#     sitting there the whole time (notebooks/99_check_rls_members.py).
#
# XMLA/TOM is therefore the only surface that tells the truth about who is in an
# RLS role. It is expensive — one connection per workspace and it needs XMLA
# read enabled on the capacity — so it is only attempted for models that
# actually declare roles. A model with no roles has no membership to miss.

ROLE_MEMBER_STATUS_OK = "Ok"


def _tom_members_for_workspace(workspace_name, model_names):
    """{modelName: {roleName: [member dicts]}} for one workspace, over XMLA."""
    import sempy.fabric as fabric

    out = {}
    server = fabric.create_tom_server(readonly=True, workspace=workspace_name)
    try:
        for model_name in model_names:
            roles = {}
            try:
                db = server.Databases.GetByName(model_name)
            except Exception:  # model not visible over XMLA
                continue
            for role in db.Model.Roles:
                members = []
                for m in role.Members:
                    # TOM reports the UPN suffixed with the identity provider,
                    # e.g. "ivana@contoso.com#AzureAD". MemberID is the Entra
                    # object id and is what everything downstream joins on.
                    name = str(m.Name or "")
                    members.append(
                        {
                            "memberName": name.split("#")[0] or None,
                            "graphId": (str(m.MemberID) or "").lower() or None,
                            "memberType": str(getattr(m, "MemberType", "") or "") or None,
                        }
                    )
                roles[str(role.Name)] = members
            out[model_name] = roles
    finally:
        try:
            server.Disconnect()
        except Exception:
            pass
    return out


# Group by workspace so we open one XMLA connection per workspace, not per model.
by_workspace = {}
for rec in model_results:
    if not rec.get("roles"):
        continue
    by_workspace.setdefault(rec.get("workspaceName"), []).append(rec)

member_lookup = {}
for ws_name, recs in by_workspace.items():
    if not ws_name:
        continue
    try:
        member_lookup[ws_name] = _tom_members_for_workspace(
            ws_name, [r.get("itemName") for r in recs if r.get("itemName")])
    except Exception as exc:  # noqa: BLE001
        # Same principle as everywhere else in OneSafe: a failed read is
        # recorded as unknown, never silently as "no members".
        for r in recs:
            r["roleMemberStatus"] = "Error"
            r["roleMemberError"] = str(exc)[:400]
        print(f"[onesafe] XMLA member read failed for '{ws_name}': {str(exc)[:200]}")

for rec in model_results:
    if not rec.get("roles"):
        continue
    if rec.get("roleMemberStatus"):
        continue
    found = (member_lookup.get(rec.get("workspaceName")) or {}).get(rec.get("itemName"))
    if found is None:
        rec["roleMemberStatus"] = "NotFound"
        continue
    rec["roleMemberStatus"] = ROLE_MEMBER_STATUS_OK
    for role in rec["roles"]:
        role["members"] = found.get(role.get("name")) or []

_member_total = sum(
    len(role.get("members") or [])
    for rec in model_results
    for role in (rec.get("roles") or [])
)
print(
    f"[onesafe] XMLA membership: {len(by_workspace)} workspace(s) probed, "
    f"{_member_total} role member(s) resolved"
)

# CELL ********************

write_bronze("model_security", model_results)

models_with_roles = sum(1 for r in model_results if r.get("roles"))
models_denied = sum(1 for r in model_results if r.get("coverageStatus") == "AccessDenied")
models_unsupported = sum(1 for r in model_results if r.get("coverageStatus") == "NotSupported")
print(
    f"[onesafe] model security: {len(model_results)} models, {models_with_roles} with roles, "
    f"{models_unsupported} without a readable definition, {models_denied} inaccessible"
)

# CELL ********************

log_run(
    "extract_onelake",
    "Succeeded",
    len(results) + len(model_results),
    f"{with_roles} items with OneLake roles, {disabled} feature-disabled, {denied} inaccessible; "
    f"{models_with_roles}/{len(model_results)} models with RLS/CLS roles",
)
print(
    f"[onesafe] onelake complete: {len(results)} items, "
    f"{with_roles} with data access roles, {disabled} with the feature disabled, "
    f"{denied} inaccessible"
)
