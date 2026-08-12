# CELL ********************

# OneSafe :: 05_transform_silver
# Parse raw bronze payloads into typed, normalised Delta tables.

# CELL ********************

# MAGIC %run 00_common

# CELL ********************

from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    IntegerType,
    StringType,
    StructField,
    StructType,
)

ensure_schemas()

# CELL ********************

# ---------------------------------------------------------------- workspaces

ws_raw = read_bronze("workspaces")

ws_rows = [
    {
        "workspace_id": (w.get("id") or "").lower(),
        "workspace_name": w.get("name"),
        "workspace_type": w.get("type"),
        "state": w.get("state"),
        "capacity_id": (w.get("capacityId") or "").lower() or None,
        "is_personal": (w.get("type") == "Personal"),
    }
    for w in ws_raw
    if w.get("id")
]

ws_schema = StructType(
    [
        StructField("workspace_id", StringType()),
        StructField("workspace_name", StringType()),
        StructField("workspace_type", StringType()),
        StructField("state", StringType()),
        StructField("capacity_id", StringType()),
        StructField("is_personal", BooleanType()),
    ]
)
df_ws = with_snapshot(spark.createDataFrame(ws_rows, ws_schema))
append_snapshot(df_ws, SILVER, "workspaces")

# CELL ********************

# ---------------------------------------------------------------- capacities

cap_raw = read_bronze("capacities")
cap_rows = [
    {
        "capacity_id": (c.get("id") or "").lower(),
        "capacity_name": c.get("displayName") or c.get("name"),
        "sku": c.get("sku"),
        "region": c.get("region"),
        "state": c.get("state"),
    }
    for c in cap_raw
    if c.get("id")
]
cap_schema = StructType(
    [
        StructField("capacity_id", StringType()),
        StructField("capacity_name", StringType()),
        StructField("sku", StringType()),
        StructField("region", StringType()),
        StructField("state", StringType()),
    ]
)
df_cap = with_snapshot(spark.createDataFrame(cap_rows, cap_schema))
append_snapshot(df_cap, SILVER, "capacities")

# CELL ********************

# ---------------------------------------------------------------- workspace roles

wsu_raw = read_bronze("workspace_users")

role_rows = []
for rec in wsu_raw:
    ws_id = (rec.get("workspaceId") or "").lower()
    for detail in rec.get("accessDetails") or []:
        p = detail.get("principal") or {}
        access = detail.get("workspaceAccessDetails") or {}
        upn = ((p.get("userDetails") or {}).get("userPrincipalName")) or None
        role_rows.append(
            {
                "workspace_id": ws_id,
                "principal_id": (p.get("id") or "").lower(),
                "principal_name": p.get("displayName"),
                "principal_type": p.get("type"),
                "principal_upn": upn,
                "workspace_role": access.get("workspaceRole"),
            }
        )

role_schema = StructType(
    [
        StructField("workspace_id", StringType()),
        StructField("principal_id", StringType()),
        StructField("principal_name", StringType()),
        StructField("principal_type", StringType()),
        StructField("principal_upn", StringType()),
        StructField("workspace_role", StringType()),
    ]
)
df_roles = with_snapshot(spark.createDataFrame(role_rows, role_schema))
append_snapshot(df_roles, SILVER, "workspace_roles")

# CELL ********************

# ---------------------------------------------------------------- items + item permissions
#
# The scanner returns Fabric item types as PascalCase keys ("Lakehouse",
# "Notebook", ...) and legacy Power BI collections as lowercase plurals
# ("reports", "datasets", ...). Rather than hard-code an ever-growing list, we
# treat any key whose value is a list of objects carrying an "id" as an item
# collection. New Fabric item types are then picked up automatically.

scan_raw = read_bronze("scan_workspaces")

# Workspace-level keys that are metadata, not item collections.
NON_ITEM_KEYS = {
    "id",
    "name",
    "type",
    "state",
    "isOnDedicatedCapacity",
    "capacityId",
    "defaultDatasetStorageFormat",
    "description",
    "users",
    "reportsCount",
    "hasWorkspaceLevelSettings",
}

# Legacy plural collection -> canonical singular item type.
LEGACY_TYPE_MAP = {
    "reports": "Report",
    "dashboards": "Dashboard",
    "datasets": "SemanticModel",
    "dataflows": "Dataflow",
    "datamarts": "Datamart",
    "warehouses": "Warehouse",
}

# Each item family names its permission field differently.
ACCESS_FIELDS = (
    "artifactUserAccessRight",
    "datasetUserAccessRight",
    "reportUserAccessRight",
    "dashboardUserAccessRight",
    "dataflowUserAccessRight",
    "datamartUserAccessRight",
    "groupUserAccessRight",
)


def access_right(user_obj):
    for field in ACCESS_FIELDS:
        if user_obj.get(field):
            return user_obj[field]
    return None


item_rows = []
item_perm_rows = []
rls_rows = []

for ws in scan_raw:
    ws_id = (ws.get("id") or "").lower()

    for key, value in ws.items():
        if key in NON_ITEM_KEYS or not isinstance(value, list):
            continue
        if not value or not isinstance(value[0], dict):
            continue

        item_type = LEGACY_TYPE_MAP.get(key, key)

        for item in value:
            item_id = (item.get("id") or "").lower()
            if not item_id:
                continue

            item_rows.append(
                {
                    "item_id": item_id,
                    "workspace_id": ws_id,
                    "item_type": item_type,
                    "item_name": item.get("name"),
                    "description": item.get("description"),
                    "created_by_id": (item.get("createdById") or "").lower() or None,
                    "created_by": item.get("createdBy") or item.get("configuredBy"),
                    "created_date": item.get("createdDate"),
                    "modified_date": item.get("lastUpdatedDate")
                    or item.get("modifiedDateTime"),
                    "state": item.get("state"),
                    "endorsement": ((item.get("endorsementDetails") or {}).get("endorsement")),
                    "sensitivity_label_id": (
                        (item.get("sensitivityLabel") or {}).get("labelId")
                    ),
                }
            )

            for u in item.get("users") or []:
                if not isinstance(u, dict):
                    continue
                item_perm_rows.append(
                    {
                        "item_id": item_id,
                        "workspace_id": ws_id,
                        "item_type": item_type,
                        "principal_id": (u.get("graphId") or "").lower() or None,
                        "principal_name": u.get("displayName"),
                        "principal_upn": u.get("emailAddress") or u.get("identifier"),
                        "principal_type": u.get("principalType"),
                        "user_type": u.get("userType"),
                        "access_right": access_right(u),
                    }
                )

            # Row-level security roles live on semantic models only.
            for role in item.get("rowLevelSecurity") or []:
                members = role.get("members") or []
                if not members:
                    rls_rows.append(
                        {
                            "item_id": item_id,
                            "workspace_id": ws_id,
                            "rls_role": role.get("name"),
                            "principal_id": None,
                            "principal_upn": None,
                            "member_type": None,
                            "table_count": len(role.get("tables") or []),
                        }
                    )
                for m in members:
                    rls_rows.append(
                        {
                            "item_id": item_id,
                            "workspace_id": ws_id,
                            "rls_role": role.get("name"),
                            "principal_id": (m.get("graphId") or "").lower() or None,
                            "principal_upn": m.get("memberName"),
                            "member_type": m.get("memberType"),
                            "table_count": len(role.get("tables") or []),
                        }
                    )

print(
    f"[onesafe] parsed {len(item_rows)} items, {len(item_perm_rows)} item grants, "
    f"{len(rls_rows)} RLS entries"
)

# CELL ********************

item_schema = StructType(
    [
        StructField("item_id", StringType()),
        StructField("workspace_id", StringType()),
        StructField("item_type", StringType()),
        StructField("item_name", StringType()),
        StructField("description", StringType()),
        StructField("created_by_id", StringType()),
        StructField("created_by", StringType()),
        StructField("created_date", StringType()),
        StructField("modified_date", StringType()),
        StructField("state", StringType()),
        StructField("endorsement", StringType()),
        StructField("sensitivity_label_id", StringType()),
    ]
)
df_items = with_snapshot(spark.createDataFrame(item_rows, item_schema)).dropDuplicates(
    ["item_id", "snapshot_date"]
)
append_snapshot(df_items, SILVER, "items")

perm_schema = StructType(
    [
        StructField("item_id", StringType()),
        StructField("workspace_id", StringType()),
        StructField("item_type", StringType()),
        StructField("principal_id", StringType()),
        StructField("principal_name", StringType()),
        StructField("principal_upn", StringType()),
        StructField("principal_type", StringType()),
        StructField("user_type", StringType()),
        StructField("access_right", StringType()),
    ]
)
df_perms = with_snapshot(spark.createDataFrame(item_perm_rows, perm_schema))
append_snapshot(df_perms, SILVER, "item_permissions")

rls_schema = StructType(
    [
        StructField("item_id", StringType()),
        StructField("workspace_id", StringType()),
        StructField("rls_role", StringType()),
        StructField("principal_id", StringType()),
        StructField("principal_upn", StringType()),
        StructField("member_type", StringType()),
        StructField("table_count", IntegerType()),
    ]
)
# NOTE: rls_role_members is written further down, after the model-security
# section has had its chance to contribute. The scanner path above is kept
# because it costs nothing, but in practice it yields nothing: the Scanner API
# never populates rowLevelSecurity[]. XMLA/TOM is the real source.

# CELL ********************

# ---------------------------------------------------------------- OneLake security

ol_raw = read_bronze("onelake_roles")

ol_role_rows = []
ol_member_rows = []
ol_rule_rows = []
ol_constraint_rows = []
ol_coverage_rows = []

for rec in ol_raw:
    ws_id = (rec.get("workspaceId") or "").lower()
    item_id = (rec.get("itemId") or "").lower()

    ol_coverage_rows.append(
        {
            "workspace_id": ws_id,
            "item_id": item_id,
            "item_type": rec.get("itemType"),
            "access_denied": bool(rec.get("accessDenied")),
            "coverage_status": rec.get("coverageStatus")
            or ("AccessDenied" if rec.get("accessDenied") else "Ok"),
            "role_count": len(rec.get("roles") or []),
            "error": (rec.get("error") or "")[:500] or None,
        }
    )

    for role in rec.get("roles") or []:
        role_id = (role.get("id") or f"{item_id}:{role.get('name')}").lower()
        ol_role_rows.append(
            {
                "role_id": role_id,
                "item_id": item_id,
                "workspace_id": ws_id,
                "role_name": role.get("name"),
                "is_default": (role.get("name") or "").lower() == "defaultreader",
            }
        )

        for rule in role.get("decisionRules") or []:
            # A rule's scope is expressed as attribute name/value pairs; "Path"
            # carries the OneLake path and "Action" the granted permission.
            attrs = {
                a.get("attributeName"): a.get("attributeValueIncludedIn") or []
                for a in (rule.get("permission") or [])
            }
            paths = attrs.get("Path") or ["*"]
            actions = attrs.get("Action") or []
            for path in paths:
                ol_rule_rows.append(
                    {
                        "role_id": role_id,
                        "item_id": item_id,
                        "effect": rule.get("effect"),
                        "path": path,
                        "permissions": ",".join(actions) if actions else None,
                    }
                )

            # OneLake row- and column-level security. These sit inside the rule
            # as `constraints`, and they are the difference between "can read
            # the table" and "can read three columns of it, for their own rows".
            constraints = rule.get("constraints") or {}
            for col_rule in constraints.get("columns") or []:
                names = col_rule.get("columnNames") or []
                ol_constraint_rows.append(
                    {
                        "role_id": role_id,
                        "item_id": item_id,
                        "workspace_id": ws_id,
                        "constraint_type": "Column",
                        "table_path": col_rule.get("tablePath"),
                        "effect": col_rule.get("columnEffect") or rule.get("effect"),
                        "columns": ",".join(names) if names else None,
                        "column_count": len(names),
                        "rule_expression": None,
                        "actions": ",".join(col_rule.get("columnAction") or []) or None,
                    }
                )
            for row_rule in constraints.get("rows") or []:
                ol_constraint_rows.append(
                    {
                        "role_id": role_id,
                        "item_id": item_id,
                        "workspace_id": ws_id,
                        "constraint_type": "Row",
                        "table_path": row_rule.get("tablePath"),
                        "effect": rule.get("effect"),
                        "columns": None,
                        "column_count": 0,
                        "rule_expression": (row_rule.get("value") or "")[:2000] or None,
                        "actions": None,
                    }
                )

        members = role.get("members") or {}
        for m in members.get("microsoftEntraMembers") or []:
            ol_member_rows.append(
                {
                    "role_id": role_id,
                    "item_id": item_id,
                    "principal_id": (m.get("objectId") or "").lower(),
                    "principal_type": m.get("objectType"),
                    "source_type": "EntraMember",
                    "source_path": None,
                }
            )
        for m in members.get("fabricItemMembers") or []:
            # Item members inherit access from another Fabric item rather than
            # naming a principal directly.
            ol_member_rows.append(
                {
                    "role_id": role_id,
                    "item_id": item_id,
                    "principal_id": None,
                    "principal_type": "FabricItem",
                    "source_type": "ItemAccess",
                    "source_path": m.get("sourcePath"),
                }
            )

ol_role_schema = StructType(
    [
        StructField("role_id", StringType()),
        StructField("item_id", StringType()),
        StructField("workspace_id", StringType()),
        StructField("role_name", StringType()),
        StructField("is_default", BooleanType()),
    ]
)
ol_member_schema = StructType(
    [
        StructField("role_id", StringType()),
        StructField("item_id", StringType()),
        StructField("principal_id", StringType()),
        StructField("principal_type", StringType()),
        StructField("source_type", StringType()),
        StructField("source_path", StringType()),
    ]
)
ol_rule_schema = StructType(
    [
        StructField("role_id", StringType()),
        StructField("item_id", StringType()),
        StructField("effect", StringType()),
        StructField("path", StringType()),
        StructField("permissions", StringType()),
    ]
)
ol_cov_schema = StructType(
    [
        StructField("workspace_id", StringType()),
        StructField("item_id", StringType()),
        StructField("item_type", StringType()),
        StructField("access_denied", BooleanType()),
        StructField("coverage_status", StringType()),
        StructField("role_count", IntegerType()),
        StructField("error", StringType()),
    ]
)

append_snapshot(with_snapshot(spark.createDataFrame(ol_role_rows, ol_role_schema)), SILVER, "onelake_roles")
append_snapshot(with_snapshot(spark.createDataFrame(ol_member_rows, ol_member_schema)), SILVER, "onelake_role_members")
append_snapshot(with_snapshot(spark.createDataFrame(ol_rule_rows, ol_rule_schema)), SILVER, "onelake_rules")
append_snapshot(with_snapshot(spark.createDataFrame(ol_coverage_rows, ol_cov_schema)), SILVER, "onelake_coverage")

ol_constraint_schema = StructType(
    [
        StructField("role_id", StringType()),
        StructField("item_id", StringType()),
        StructField("workspace_id", StringType()),
        StructField("constraint_type", StringType()),
        StructField("table_path", StringType()),
        StructField("effect", StringType()),
        StructField("columns", StringType()),
        StructField("column_count", IntegerType()),
        StructField("rule_expression", StringType()),
        StructField("actions", StringType()),
    ]
)
append_snapshot(
    with_snapshot(spark.createDataFrame(ol_constraint_rows, ol_constraint_schema)),
    SILVER,
    "onelake_constraints",
)
print(f"[onesafe] {len(ol_constraint_rows)} OneLake row/column constraints")

# CELL ********************

# ------------------------------------------------- semantic model RLS / CLS rules
#
# Membership and rules come from two different APIs and neither has both:
#
#   scanner        -> role NAME and MEMBERS, no rules
#   getDefinition  -> role NAME and RULES, members stripped by the service
#
# So they are landed separately and joined on (item_id, role_name) in gold.
# Getting this backwards produces the worst possible output for a security tool:
# a role that looks empty, or a member with no visible restriction.

ms_raw = read_bronze("model_security")

ms_role_rows = []
ms_rule_rows = []
ms_coverage_rows = []

for rec in ms_raw or []:
    ws_id = (rec.get("workspaceId") or "").lower()
    item_id = (rec.get("itemId") or "").lower()
    roles = rec.get("roles") or []

    rls_rules = 0
    cls_rules = 0

    for role in roles:
        role_name = role.get("name")
        table_perms = role.get("tablePermissions") or []
        role_rls = 0
        role_cls = 0

        for tp in table_perms:
            table_name = tp.get("name")
            expr = (tp.get("filterExpression") or "").strip()
            if expr:
                role_rls += 1
                ms_rule_rows.append(
                    {
                        "item_id": item_id,
                        "workspace_id": ws_id,
                        "role_name": role_name,
                        "rule_type": "RLS",
                        "table_name": table_name,
                        "column_name": None,
                        "rule_expression": expr[:2000],
                        # A filter that references the caller resolves per user;
                        # a static one does not. Admins triage these differently.
                        "is_dynamic": ("USERPRINCIPALNAME" in expr.upper()
                                       or "USERNAME(" in expr.upper()
                                       or "CUSTOMDATA" in expr.upper()),
                        "permission": None,
                    }
                )

            for cp in tp.get("columnPermissions") or []:
                perm = str(cp.get("metadataPermission") or "").lower()
                # "read" is the default and restricts nothing; only "none"
                # (the column is hidden) is column-level security.
                if perm and perm != "none":
                    continue
                role_cls += 1
                ms_rule_rows.append(
                    {
                        "item_id": item_id,
                        "workspace_id": ws_id,
                        "role_name": role_name,
                        "rule_type": "CLS",
                        "table_name": table_name,
                        "column_name": cp.get("name"),
                        "rule_expression": None,
                        "is_dynamic": False,
                        "permission": perm or "none",
                    }
                )

        rls_rules += role_rls
        cls_rules += role_cls

        # Membership comes from XMLA/TOM (see 03_extract_onelake.py) and lands
        # in the same silver table the scanner path targets. A role with no
        # members still gets a row with a null principal — an unassigned RLS
        # role is a finding, not an absence.
        role_members = role.get("members") or []
        if not role_members:
            rls_rows.append(
                {
                    "item_id": item_id,
                    "workspace_id": ws_id,
                    "rls_role": role_name,
                    "principal_id": None,
                    "principal_upn": None,
                    "member_type": None,
                    "table_count": len(table_perms),
                }
            )
        for m in role_members:
            rls_rows.append(
                {
                    "item_id": item_id,
                    "workspace_id": ws_id,
                    "rls_role": role_name,
                    "principal_id": (m.get("graphId") or "").lower() or None,
                    "principal_upn": m.get("memberName"),
                    "member_type": m.get("memberType") or "User",
                    "table_count": len(table_perms),
                }
            )

        ms_role_rows.append(
            {
                "item_id": item_id,
                "workspace_id": ws_id,
                "role_name": role_name,
                "description": (role.get("description") or "")[:500] or None,
                "model_permission": role.get("modelPermission"),
                "table_count": len(table_perms),
                "rls_rule_count": role_rls,
                "cls_rule_count": role_cls,
                "member_count": len(role_members),
            }
        )

    ms_coverage_rows.append(
        {
            "workspace_id": ws_id,
            "item_id": item_id,
            "coverage_status": rec.get("coverageStatus") or "Ok",
            "role_count": len(roles),
            "rls_rule_count": rls_rules,
            "cls_rule_count": cls_rules,
            "error": (rec.get("error") or "")[:500] or None,
        }
    )

ms_role_schema = StructType(
    [
        StructField("item_id", StringType()),
        StructField("workspace_id", StringType()),
        StructField("role_name", StringType()),
        StructField("description", StringType()),
        StructField("model_permission", StringType()),
        StructField("table_count", IntegerType()),
        StructField("rls_rule_count", IntegerType()),
        StructField("cls_rule_count", IntegerType()),
        StructField("member_count", IntegerType()),
    ]
)
ms_rule_schema = StructType(
    [
        StructField("item_id", StringType()),
        StructField("workspace_id", StringType()),
        StructField("role_name", StringType()),
        StructField("rule_type", StringType()),
        StructField("table_name", StringType()),
        StructField("column_name", StringType()),
        StructField("rule_expression", StringType()),
        StructField("is_dynamic", BooleanType()),
        StructField("permission", StringType()),
    ]
)
ms_cov_schema = StructType(
    [
        StructField("workspace_id", StringType()),
        StructField("item_id", StringType()),
        StructField("coverage_status", StringType()),
        StructField("role_count", IntegerType()),
        StructField("rls_rule_count", IntegerType()),
        StructField("cls_rule_count", IntegerType()),
        StructField("error", StringType()),
    ]
)

append_snapshot(with_snapshot(spark.createDataFrame(ms_role_rows, ms_role_schema)), SILVER, "model_security_roles")
append_snapshot(with_snapshot(spark.createDataFrame(ms_rule_rows, ms_rule_schema)), SILVER, "model_security_rules")
append_snapshot(with_snapshot(spark.createDataFrame(ms_coverage_rows, ms_cov_schema)), SILVER, "model_security_coverage")

# Now that model security has contributed its XMLA-sourced members, write the
# combined RLS membership table.
df_rls = with_snapshot(spark.createDataFrame(rls_rows, rls_schema))
append_snapshot(df_rls, SILVER, "rls_role_members")

print(
    f"[onesafe] model security: {len(ms_role_rows)} roles, {len(ms_rule_rows)} rules "
    f"across {len(ms_coverage_rows)} models; {len(rls_rows)} RLS membership rows"
)

# CELL ********************

# ---------------------------------------------------------------- principals

gp_raw = read_bronze("graph_principals")
unresolved_raw = read_bronze("graph_unresolved")

ODATA_TYPE_MAP = {
    "#microsoft.graph.user": "User",
    "#microsoft.graph.group": "Group",
    "#microsoft.graph.servicePrincipal": "ServicePrincipal",
}

principal_rows = []
for p in gp_raw:
    ptype = ODATA_TYPE_MAP.get(p.get("@odata.type"), "Unknown")
    principal_rows.append(
        {
            "principal_id": (p.get("id") or "").lower(),
            "display_name": p.get("displayName"),
            "upn": p.get("userPrincipalName") or p.get("mail"),
            "principal_type": ptype,
            "account_enabled": p.get("accountEnabled"),
            "department": p.get("department"),
            "job_title": p.get("jobTitle"),
            # #EXT# in the UPN is the canonical marker for a B2B guest account.
            "is_guest": (str(p.get("userType") or "").lower() == "guest")
            or ("#EXT#" in (p.get("userPrincipalName") or "")),
            "app_id": p.get("appId"),
            "is_resolved": True,
        }
    )

for u in unresolved_raw:
    principal_rows.append(
        {
            "principal_id": (u.get("id") or "").lower(),
            "display_name": None,
            "upn": None,
            "principal_type": u.get("typeHint") or "Unknown",
            "account_enabled": None,
            "department": None,
            "job_title": None,
            "is_guest": False,
            "app_id": None,
            "is_resolved": False,
        }
    )

principal_schema = StructType(
    [
        StructField("principal_id", StringType()),
        StructField("display_name", StringType()),
        StructField("upn", StringType()),
        StructField("principal_type", StringType()),
        StructField("account_enabled", BooleanType()),
        StructField("department", StringType()),
        StructField("job_title", StringType()),
        StructField("is_guest", BooleanType()),
        StructField("app_id", StringType()),
        StructField("is_resolved", BooleanType()),
    ]
)
df_principals = with_snapshot(
    spark.createDataFrame(principal_rows, principal_schema)
).dropDuplicates(["principal_id", "snapshot_date"])
append_snapshot(df_principals, SILVER, "principals")

# CELL ********************

# ---------------------------------------------------------------- group membership

gm_raw = read_bronze("graph_group_members")

member_rows = []
group_meta_rows = []
for g in gm_raw:
    gid = (g.get("groupId") or "").lower()
    group_meta_rows.append(
        {
            "group_id": gid,
            "member_count": int(g.get("memberCount") or 0),
            "is_broad": bool(g.get("isBroad")),
            "error": (g.get("error") or "")[:400] or None,
        }
    )
    for m in g.get("members") or []:
        member_rows.append(
            {
                "group_id": gid,
                "member_id": (m.get("id") or "").lower(),
                "member_name": m.get("displayName"),
                "member_upn": m.get("userPrincipalName"),
                "member_type": m.get("type"),
            }
        )

member_schema = StructType(
    [
        StructField("group_id", StringType()),
        StructField("member_id", StringType()),
        StructField("member_name", StringType()),
        StructField("member_upn", StringType()),
        StructField("member_type", StringType()),
    ]
)
group_meta_schema = StructType(
    [
        StructField("group_id", StringType()),
        StructField("member_count", IntegerType()),
        StructField("is_broad", BooleanType()),
        StructField("error", StringType()),
    ]
)

append_snapshot(with_snapshot(spark.createDataFrame(member_rows, member_schema)), SILVER, "group_members")
append_snapshot(with_snapshot(spark.createDataFrame(group_meta_rows, group_meta_schema)), SILVER, "groups")

# CELL ********************

log_run(
    "transform_silver",
    "Succeeded",
    len(item_rows) + len(item_perm_rows) + len(member_rows),
    f"items={len(item_rows)} grants={len(item_perm_rows)} members={len(member_rows)}",
)
print("[onesafe] silver transform complete")
