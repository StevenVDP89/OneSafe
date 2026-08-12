# CELL ********************

# OneSafe :: 01_extract_inventory
# Workspaces, capacities, workspace role assignments and tenant settings.

# CELL ********************

# MAGIC %run 00_common

# CELL ********************

ensure_schemas()

# CELL ********************

# ---------------------------------------------------------------- workspaces

workspaces = paged_get(
    f"{FABRIC_API}/v1/admin/workspaces",
    RES_FABRIC,
    collection="workspaces",
    params={"$top": 5000},
)
write_bronze("workspaces", workspaces)
log_run("extract_inventory.workspaces", "Succeeded", len(workspaces))

# CELL ********************

# ---------------------------------------------------------------- capacities

# The Fabric admin surface has no capacities route; the Power BI admin endpoint
# is authoritative and additionally returns the capacity admin list.
capacities = (
    api_get(f"{POWERBI_API}/v1.0/myorg/admin/capacities", RES_POWERBI) or {}
).get("value") or []
write_bronze("capacities", capacities)
log_run("extract_inventory.capacities", "Succeeded", len(capacities))

# CELL ********************

# ---------------------------------------------------------------- workspace users

from concurrent.futures import ThreadPoolExecutor, as_completed


def fetch_workspace_users(ws):
    ws_id = ws.get("id")
    try:
        # 403/404 are expected for deleted or otherwise inaccessible workspaces.
        resp = api_get(
            f"{FABRIC_API}/v1/admin/workspaces/{ws_id}/users",
            RES_FABRIC,
            tolerate=(403, 404),
        )
        if resp is None:
            return {"workspaceId": ws_id, "accessDenied": True, "accessDetails": []}
        return {
            "workspaceId": ws_id,
            "accessDenied": False,
            "accessDetails": resp.get("accessDetails") or [],
        }
    except ApiError as exc:
        return {
            "workspaceId": ws_id,
            "accessDenied": True,
            "error": str(exc)[:400],
            "accessDetails": [],
        }


ws_users = []
# Bounded concurrency keeps us inside the admin API throttling envelope.
with ThreadPoolExecutor(max_workers=8) as pool:
    futures = [pool.submit(fetch_workspace_users, w) for w in workspaces]
    for i, fut in enumerate(as_completed(futures), 1):
        ws_users.append(fut.result())
        if i % 100 == 0:
            print(f"[onesafe] workspace users {i}/{len(workspaces)}")

write_bronze("workspace_users", ws_users)
denied = sum(1 for r in ws_users if r.get("accessDenied"))
log_run(
    "extract_inventory.workspace_users",
    "Succeeded",
    len(ws_users),
    f"{denied} workspaces inaccessible",
)

# CELL ********************

# ---------------------------------------------------------------- tenant settings

settings = api_get(f"{FABRIC_API}/v1/admin/tenantsettings", RES_FABRIC) or {}
tenant_settings = settings.get("tenantSettings") or []
write_bronze("tenant_settings", tenant_settings)
log_run("extract_inventory.tenant_settings", "Succeeded", len(tenant_settings))

# CELL ********************

print(
    f"[onesafe] inventory complete: {len(workspaces)} workspaces, "
    f"{len(capacities)} capacities, {denied} inaccessible"
)
