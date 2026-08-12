# CELL ********************

# OneSafe :: 98_seed_demo_rls
# One-off (idempotent) seeder: attaches Entra members to the RLS/CLS roles on
# the demo semantic model.
#
# Why a notebook rather than a REST call
# -------------------------------------
# Role *rules* (filter expressions and column permissions) deploy fine through
# the Fabric semantic-model definition API, but role *membership* does not - the
# service strips `roles[].members[]` from an uploaded model.bim, and there is no
# REST endpoint for it. The only supported programmatic path is the XMLA
# endpoint via TOM, which needs the Analysis Services client libraries. Those
# ship inside Fabric notebooks (semantic-link), so this runs there.
#
# It is NOT part of the daily pipeline - it is demo setup, run on demand:
#   python tools/run_notebooks.py 98_seed_demo_rls

# CELL ********************

%run 00_common

# CELL ********************

# Demo sandbox settings come from the runtime config in OneLake (written by
# tools/upload_config.py), so this notebook carries no tenant-specific ids.
DEMO_WORKSPACE_ID = CONFIG.get("demoWorkspaceId")
DEMO_MODEL = "sm_onesafe_demo"

_principals = CONFIG.get("demoPrincipals") or []
if not DEMO_WORKSPACE_ID or len(_principals) < 2:
    raise SystemExit(
        "demoWorkspaceId and at least 2 demoPrincipals must be present in the "
        "runtime config.\n"
        "Run `python tools/setup.py --with-demo` then `python tools/upload_config.py --sync`."
    )

# Entra object IDs and UPNs. Both are required: TOM stores the object ID as the
# durable identity and the UPN as the display/lookup name.
PRINCIPAL_A = (_principals[0]["objectId"], _principals[0]["upn"])
PRINCIPAL_B = (_principals[1]["objectId"], _principals[1]["upn"])

DESIRED_MEMBERS = {
    "DemoRegionEMEA": [PRINCIPAL_A],
    "DemoRegionAMER": [PRINCIPAL_B],
    "DemoNoPII": [PRINCIPAL_B],
    "DemoAuditReadOnly": [PRINCIPAL_A],
}

# CELL ********************

import sempy.fabric as fabric

# create_tom_server returns a live AMO/TOM connection over the workspace XMLA
# endpoint. readonly=False is required to persist changes.
server = fabric.create_tom_server(readonly=False, workspace=DEMO_WORKSPACE_ID)
print("[onesafe] connected:", server.Name)
print("[onesafe] databases:", [db.Name for db in server.Databases])

# CELL ********************

from Microsoft.AnalysisServices.Tabular import ExternalModelRoleMember

db = server.Databases.GetByName(DEMO_MODEL)
model = db.Model

added, kept = 0, 0
for role in model.Roles:
    wanted = DESIRED_MEMBERS.get(role.Name)
    if not wanted:
        continue
    existing = {(m.MemberID or "").lower() for m in role.Members}
    for object_id, upn in wanted:
        if object_id.lower() in existing:
            kept += 1
            continue
        m = ExternalModelRoleMember()
        m.MemberName = upn
        m.MemberID = object_id
        m.IdentityProvider = "AzureAD"
        role.Members.Add(m)
        added += 1
        print(f"[onesafe] + {role.Name} <- {upn}")

if added:
    model.SaveChanges()
    print(f"[onesafe] saved: {added} member(s) added, {kept} already present")
else:
    print(f"[onesafe] nothing to do: {kept} member(s) already present")

# CELL ********************

# Read back through a fresh connection so the assertion reflects the service,
# not the in-memory object graph we just mutated.
verify = fabric.create_tom_server(readonly=True, workspace=DEMO_WORKSPACE_ID)
vdb = verify.Databases.GetByName(DEMO_MODEL)
total = 0
for role in vdb.Model.Roles:
    members = [m.MemberName for m in role.Members]
    total += len(members)
    rls = [f"{tp.Name}: {tp.FilterExpression}" for tp in role.TablePermissions if tp.FilterExpression]
    cls = [
        f"{tp.Name}.{cp.Name}"
        for tp in role.TablePermissions
        for cp in tp.ColumnPermissions
        if str(cp.MetadataPermission) == "None"
    ]
    print(f"  {role.Name}: members={members} rls={rls} cls={cls}")

if total == 0:
    raise RuntimeError("No role members persisted - the demo model will not link to principals.")
print(f"[onesafe] demo RLS/CLS seeded: {total} role member(s)")
