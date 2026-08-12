# CELL ********************

# OneSafe :: 04_extract_graph
# Entra principal directory plus transitive group expansion.
#
# Most Fabric permissions are granted to security groups, so resolving
# "what can this user reach?" is impossible without expanding memberships.
# Only groups actually referenced by a Fabric grant are expanded, which keeps
# the volume proportional to the security surface rather than the directory.

# CELL ********************

# MAGIC %run 00_common

# CELL ********************

from concurrent.futures import ThreadPoolExecutor, as_completed

# Groups above this size are recorded as "broad" and not materialised member by
# member - expanding them would dominate the fact table without adding insight.
BROAD_GROUP_THRESHOLD = 5000

workspace_users = read_bronze("workspace_users")
scan_workspaces = read_bronze("scan_workspaces")

# CELL ********************

# ---------------------------------------------------------------- referenced principals

referenced = {}   # principalId -> type hint


def note(pid, ptype):
    if not pid:
        return
    pid = str(pid).lower()
    if pid not in referenced or not referenced[pid]:
        referenced[pid] = ptype


for rec in workspace_users:
    for detail in rec.get("accessDetails") or []:
        p = detail.get("principal") or {}
        note(p.get("id"), p.get("type"))

# Scanner user entries expose graphId + principalType on every item collection.
def walk_users(node):
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "users" and isinstance(val, list):
                for u in val:
                    if isinstance(u, dict):
                        note(u.get("graphId"), u.get("principalType"))
            else:
                walk_users(val)
    elif isinstance(node, list):
        for v in node:
            walk_users(v)


walk_users(scan_workspaces)

# OneLake roles reference Entra objects directly.
for rec in read_bronze("onelake_roles"):
    for role in rec.get("roles") or []:
        members = role.get("members") or {}
        for m in members.get("microsoftEntraMembers") or []:
            note(m.get("objectId"), m.get("objectType"))

print(f"[onesafe] {len(referenced)} distinct principals referenced by Fabric grants")

# CELL ********************

# ---------------------------------------------------------------- resolve principals

# directoryObjects/getByIds resolves users, groups and service principals in one
# call and tolerates ids the caller cannot see.
def resolve_batch(ids):
    body = {
        "ids": ids,
        "types": ["user", "group", "servicePrincipal"],
    }
    try:
        resp = api_post(
            f"{GRAPH_API}/v1.0/directoryObjects/getByIds",
            RES_GRAPH,
            json_body=body,
            tolerate=(400, 403),
        )
        return (resp or {}).get("value") or []
    except ApiError as exc:
        print(f"[onesafe] getByIds batch failed: {exc}")
        return []


guid_ids = [p for p in referenced if len(p) == 36 and "-" in p]
batches = [guid_ids[i : i + 900] for i in range(0, len(guid_ids), 900)]

principals = []
with ThreadPoolExecutor(max_workers=4) as pool:
    for res in pool.map(resolve_batch, batches):
        principals.extend(res)

resolved_ids = {str(p.get("id")).lower() for p in principals}
print(f"[onesafe] resolved {len(principals)}/{len(guid_ids)} principals from Graph")

# Principals that could not be resolved are usually deleted accounts still
# holding permissions - a genuine finding, so record them explicitly.
unresolved = [
    {"id": pid, "typeHint": referenced.get(pid)}
    for pid in guid_ids
    if pid not in resolved_ids
]

write_bronze("graph_principals", principals)
write_bronze("graph_unresolved", unresolved)

# CELL ********************

# ---------------------------------------------------------------- transitive membership

group_ids = [
    str(p["id"]).lower()
    for p in principals
    if p.get("@odata.type", "").endswith("group")
]
print(f"[onesafe] expanding {len(group_ids)} referenced groups")


def expand_group(gid):
    url = f"{GRAPH_API}/v1.0/groups/{gid}/transitiveMembers"
    members = []
    params = {"$select": "id,displayName,userPrincipalName", "$top": 999}
    next_url = url
    try:
        while next_url:
            page = api_get(next_url, RES_GRAPH, params=params, tolerate=(403, 404))
            if page is None:
                break
            members.extend(page.get("value") or [])
            next_url = page.get("@odata.nextLink")
            params = None  # nextLink already carries the query string
            if len(members) > BROAD_GROUP_THRESHOLD:
                return {
                    "groupId": gid,
                    "isBroad": True,
                    "memberCount": len(members),
                    "members": [],
                }
    except ApiError as exc:
        return {"groupId": gid, "error": str(exc)[:300], "members": [], "isBroad": False}

    return {
        "groupId": gid,
        "isBroad": False,
        "memberCount": len(members),
        "members": [
            {
                "id": m.get("id"),
                "displayName": m.get("displayName"),
                "userPrincipalName": m.get("userPrincipalName"),
                "type": (m.get("@odata.type") or "").split(".")[-1],
            }
            for m in members
        ],
    }


memberships = []
with ThreadPoolExecutor(max_workers=6) as pool:
    futures = [pool.submit(expand_group, g) for g in group_ids]
    for i, fut in enumerate(as_completed(futures), 1):
        memberships.append(fut.result())
        if i % 25 == 0:
            print(f"[onesafe] groups expanded {i}/{len(group_ids)}")

write_bronze("graph_group_members", memberships)

# CELL ********************

broad = sum(1 for m in memberships if m.get("isBroad"))
total_members = sum(len(m.get("members") or []) for m in memberships)
log_run(
    "extract_graph",
    "Succeeded",
    len(principals),
    f"{len(memberships)} groups, {total_members} memberships, {broad} broad, {len(unresolved)} unresolved",
)
print(
    f"[onesafe] graph complete: {len(principals)} principals, "
    f"{total_members} memberships, {len(unresolved)} unresolved"
)
