# CELL ********************

# OneSafe :: 06_build_gold
# Star schema plus the effective-access resolution that powers the 360 view.
#
# Grain of fact_effective_access is one row per *access path*: a principal may
# reach the same item several ways (workspace role, direct grant, group
# membership) and admins need to see every route, not just the strongest.

# CELL ********************

# MAGIC %run 00_common

# CELL ********************

from pyspark.sql import Window
from pyspark.sql import functions as F

ensure_schemas()
SNAP = SNAPSHOT_DATE


def silver(name):
    return spark.table(f"{SILVER}.{name}").where(F.col("snapshot_date") == SNAP)


s_workspaces = silver("workspaces")
s_capacities = silver("capacities")
s_items = silver("items")
s_ws_roles = silver("workspace_roles")
s_item_perms = silver("item_permissions")
s_principals = silver("principals")
s_group_members = silver("group_members")
s_groups = silver("groups")
s_rls = silver("rls_role_members")
s_ol_roles = silver("onelake_roles")
s_ol_members = silver("onelake_role_members")
s_ol_rules = silver("onelake_rules")
s_ol_cov = silver("onelake_coverage")


def silver_optional(name, schema_cols):
    """Read a silver table that may not exist yet on an older snapshot.

    New security tables land mid-history. Failing the whole gold build because
    yesterday's snapshot predates a feature would take the entire app down for
    a purely additive change.
    """
    try:
        return silver(name)
    except Exception as exc:
        print(f"[onesafe] silver.{name} unavailable ({type(exc).__name__}) - using empty frame")
        return spark.createDataFrame(
            [], StructType([StructField(c, t) for c, t in schema_cols])
        ).withColumn("snapshot_date", F.lit(SNAP))


from pyspark.sql.types import BooleanType, IntegerType, StringType, StructField, StructType

s_ol_constraints = silver_optional(
    "onelake_constraints",
    [("role_id", StringType()), ("item_id", StringType()), ("workspace_id", StringType()),
     ("constraint_type", StringType()), ("table_path", StringType()), ("effect", StringType()),
     ("columns", StringType()), ("column_count", IntegerType()),
     ("rule_expression", StringType()), ("actions", StringType())],
)
s_ms_roles = silver_optional(
    "model_security_roles",
    [("item_id", StringType()), ("workspace_id", StringType()), ("role_name", StringType()),
     ("description", StringType()), ("model_permission", StringType()),
     ("table_count", IntegerType()), ("rls_rule_count", IntegerType()),
     ("cls_rule_count", IntegerType()), ("member_count", IntegerType())],
)
s_ms_rules = silver_optional(
    "model_security_rules",
    [("item_id", StringType()), ("workspace_id", StringType()), ("role_name", StringType()),
     ("rule_type", StringType()), ("table_name", StringType()), ("column_name", StringType()),
     ("rule_expression", StringType()), ("is_dynamic", BooleanType()), ("permission", StringType())],
)
s_ms_cov = silver_optional(
    "model_security_coverage",
    [("workspace_id", StringType()), ("item_id", StringType()), ("coverage_status", StringType()),
     ("role_count", IntegerType()), ("rls_rule_count", IntegerType()),
     ("cls_rule_count", IntegerType()), ("error", StringType())],
)

# CELL ********************

# ---------------------------------------------------------------- permission ranking
#
# Fabric expresses item rights as concatenated tokens ("ReadWriteReshareExecute")
# and workspace roles as a single name. Both are mapped onto one ordered scale so
# that "strongest access wins" comparisons are possible across sources.

PERMISSION_LEVELS = {
    "None": 0,
    "Read": 1,
    "Build": 2,
    "Write": 3,
    "Reshare": 4,
    "Admin": 5,
}

WORKSPACE_ROLE_LEVEL = {
    "Admin": 5,
    "Member": 4,
    "Contributor": 3,
    "Viewer": 1,
}


def parse_access_right(col):
    """Map a concatenated access-right token string onto the permission scale."""
    return (
        F.when(col.isNull(), F.lit(0))
        .when(col.contains("Owner"), F.lit(5))
        .when(col.contains("Reshare"), F.lit(4))
        .when(col.contains("Write"), F.lit(3))
        .when(col.contains("Build") | col.contains("Explore"), F.lit(2))
        .when(col.contains("Read") | col.contains("Execute"), F.lit(1))
        .otherwise(F.lit(0))
    )


def level_to_name(col):
    return (
        F.when(col >= 5, F.lit("Admin"))
        .when(col == 4, F.lit("Reshare"))
        .when(col == 3, F.lit("Write"))
        .when(col == 2, F.lit("Build"))
        .when(col == 1, F.lit("Read"))
        .otherwise(F.lit("None"))
    )


ws_role_level_expr = F.coalesce(
    F.when(F.col("workspace_role") == "Admin", F.lit(5))
    .when(F.col("workspace_role") == "Member", F.lit(4))
    .when(F.col("workspace_role") == "Contributor", F.lit(3))
    .when(F.col("workspace_role") == "Viewer", F.lit(1)),
    F.lit(0),
)

# CELL ********************

# ---------------------------------------------------------------- dimensions
#
# Dimensions are collapsed to one row per entity, carrying the most recently
# observed attributes. History lives in the facts (which stay snapshot-dated),
# which keeps dimension keys unique and the star schema unambiguous.


def collapse_dim(df, key_cols):
    part = Window.partitionBy(*key_cols)
    return (
        df.withColumn("first_seen_date", F.min("snapshot_date").over(part))
        .withColumn(
            "_rn", F.row_number().over(part.orderBy(F.col("snapshot_date").desc()))
        )
        .where(F.col("_rn") == 1)
        .drop("_rn")
        .withColumnRenamed("snapshot_date", "last_seen_date")
    )


dim_capacity = s_capacities.select(
    "capacity_id", "capacity_name", "sku", "region", "state", "snapshot_date"
).dropDuplicates(["capacity_id", "snapshot_date"])
save_table(collapse_dim(dim_capacity, ["capacity_id"]), GOLD, "dim_capacity")

cap_lite = s_capacities.select(
    "capacity_id",
    "snapshot_date",
    F.col("capacity_name").alias("cap_name"),
    F.col("sku").alias("cap_sku"),
)

dim_workspace = (
    s_workspaces.join(cap_lite, ["capacity_id", "snapshot_date"], "left")
    .select(
        "workspace_id",
        "workspace_name",
        "workspace_type",
        "state",
        "is_personal",
        "capacity_id",
        F.coalesce(F.col("cap_name"), F.lit("(none)")).alias("capacity_name"),
        F.coalesce(F.col("cap_sku"), F.lit("(none)")).alias("capacity_sku"),
        "snapshot_date",
    )
    .dropDuplicates(["workspace_id", "snapshot_date"])
)
save_table(collapse_dim(dim_workspace, ["workspace_id"]), GOLD, "dim_workspace")

# Items that carry OneLake data access roles are flagged so the UI can show
# where data-plane restrictions narrow item-level access.
ol_secured = (
    s_ol_roles.where(~F.col("is_default"))
    .groupBy("item_id", "snapshot_date")
    .agg(F.countDistinct("role_id").alias("onelake_role_count"))
)

ws_lite = s_workspaces.select(
    "workspace_id",
    "snapshot_date",
    F.col("workspace_name").alias("ws_name"),
    F.col("is_personal").alias("ws_is_personal"),
)

# Row/column restrictions per item, from both planes. Computed here from silver
# rather than from fact_data_security because dim_item is built first, and an
# item's security posture belongs on the dimension so every pane can filter on
# it without joining the rule-grain fact.
item_rls_cls = (
    s_ms_rules.select(
        "item_id", "snapshot_date", "rule_type",
        F.coalesce(F.col("is_dynamic"), F.lit(False)).alias("is_dynamic"),
    )
    .unionByName(
        s_ol_constraints.select(
            "item_id", "snapshot_date",
            F.when(F.col("constraint_type") == "Row", F.lit("RLS"))
             .otherwise(F.lit("CLS")).alias("rule_type"),
            F.lit(False).alias("is_dynamic"),
        )
    )
    .groupBy("item_id", "snapshot_date")
    .agg(
        F.sum(F.when(F.col("rule_type") == "RLS", 1).otherwise(0)).alias("rls_rule_count"),
        F.sum(F.when(F.col("rule_type") == "CLS", 1).otherwise(0)).alias("cls_rule_count"),
        F.max(F.col("is_dynamic")).alias("has_dynamic_rls"),
    )
)

dim_item = (
    s_items.join(ws_lite, ["workspace_id", "snapshot_date"], "left")
    .join(ol_secured, ["item_id", "snapshot_date"], "left")
    .join(item_rls_cls, ["item_id", "snapshot_date"], "left")
    .select(
        "item_id",
        "workspace_id",
        F.coalesce(F.col("ws_name"), F.lit("(unknown)")).alias("workspace_name"),
        "item_type",
        "item_name",
        "description",
        "created_by",
        "created_by_id",
        "created_date",
        "modified_date",
        "endorsement",
        "sensitivity_label_id",
        F.coalesce(F.col("ws_is_personal"), F.lit(False)).alias("is_personal_workspace"),
        F.coalesce(F.col("onelake_role_count"), F.lit(0)).alias("onelake_role_count"),
        (F.coalesce(F.col("onelake_role_count"), F.lit(0)) > 0).alias("has_onelake_security"),
        F.coalesce(F.col("rls_rule_count"), F.lit(0)).alias("rls_rule_count"),
        F.coalesce(F.col("cls_rule_count"), F.lit(0)).alias("cls_rule_count"),
        (F.coalesce(F.col("rls_rule_count"), F.lit(0)) > 0).alias("has_rls"),
        (F.coalesce(F.col("cls_rule_count"), F.lit(0)) > 0).alias("has_cls"),
        F.coalesce(F.col("has_dynamic_rls"), F.lit(False)).alias("has_dynamic_rls"),
        "snapshot_date",
    )
    .dropDuplicates(["item_id", "snapshot_date"])
)
save_table(collapse_dim(dim_item, ["item_id"]), GOLD, "dim_item")

# CELL ********************

# ---------------------------------------------------------------- dim_principal
#
# Principal detail is enriched from every place a name appears, because Graph
# cannot resolve deleted accounts that still hold permissions.

name_hints = (
    s_ws_roles.select(
        F.col("principal_id"),
        F.col("principal_name").alias("hint_name"),
        F.col("principal_upn").alias("hint_upn"),
        F.col("principal_type").alias("hint_type"),
        "snapshot_date",
    )
    .unionByName(
        s_item_perms.select(
            F.col("principal_id"),
            F.col("principal_name").alias("hint_name"),
            F.col("principal_upn").alias("hint_upn"),
            F.col("principal_type").alias("hint_type"),
            "snapshot_date",
        )
    )
    .where(F.col("principal_id").isNotNull())
    .groupBy("principal_id", "snapshot_date")
    .agg(
        F.first("hint_name", ignorenulls=True).alias("hint_name"),
        F.first("hint_upn", ignorenulls=True).alias("hint_upn"),
        F.first("hint_type", ignorenulls=True).alias("hint_type"),
    )
)

p_lite = s_principals.select(
    "principal_id",
    "snapshot_date",
    F.col("display_name").alias("p_display_name"),
    F.col("upn").alias("p_upn"),
    F.col("principal_type").alias("p_principal_type"),
    F.col("account_enabled").alias("p_account_enabled"),
    F.col("department").alias("p_department"),
    F.col("job_title").alias("p_job_title"),
    F.col("is_guest").alias("p_is_guest"),
    F.col("app_id").alias("p_app_id"),
    F.col("is_resolved").alias("p_is_resolved"),
)

g_lite = s_groups.select(
    F.col("group_id").alias("principal_id"),
    "snapshot_date",
    F.col("member_count").alias("g_member_count"),
    F.col("is_broad").alias("g_is_broad"),
)

dim_principal = (
    p_lite.join(name_hints, ["principal_id", "snapshot_date"], "full_outer")
    .join(g_lite, ["principal_id", "snapshot_date"], "left")
    .select(
        F.col("principal_id"),
        F.coalesce(
            F.col("p_display_name"), F.col("hint_name"), F.lit("(unknown principal)")
        ).alias("display_name"),
        F.coalesce(F.col("p_upn"), F.col("hint_upn")).alias("upn"),
        F.coalesce(
            F.col("p_principal_type"), F.col("hint_type"), F.lit("Unknown")
        ).alias("principal_type"),
        F.col("p_account_enabled").alias("account_enabled"),
        F.col("p_department").alias("department"),
        F.col("p_job_title").alias("job_title"),
        F.coalesce(F.col("p_is_guest"), F.lit(False)).alias("is_guest"),
        F.col("p_app_id").alias("app_id"),
        F.coalesce(F.col("p_is_resolved"), F.lit(False)).alias("is_resolved"),
        F.coalesce(F.col("g_member_count"), F.lit(0)).alias("group_member_count"),
        F.coalesce(F.col("g_is_broad"), F.lit(False)).alias("is_broad_group"),
        F.col("snapshot_date"),
    )
    .withColumn(
        # A principal that no longer resolves in Entra but still holds grants is
        # an orphaned permission - one of the highest-value findings here.
        "is_orphaned",
        (~F.col("is_resolved")) | (F.col("account_enabled") == False),  # noqa: E712
    )
    .dropDuplicates(["principal_id", "snapshot_date"])
)
save_table(collapse_dim(dim_principal, ["principal_id"]), GOLD, "dim_principal")

# CELL ********************

# ---------------------------------------------------------------- bridge_group_membership

bridge_group_membership = (
    s_group_members.select(
        F.col("group_id"),
        F.col("member_id").alias("principal_id"),
        F.col("member_name"),
        F.col("member_upn"),
        F.col("member_type"),
        "snapshot_date",
    )
    .where(F.col("principal_id").isNotNull() & (F.col("principal_id") != ""))
    .dropDuplicates(["group_id", "principal_id", "snapshot_date"])
)
save_table(bridge_group_membership, GOLD, "bridge_group_membership")

# Lookup used to expand group grants into member grants.
expansion = bridge_group_membership.select(
    F.col("group_id").alias("x_group_id"),
    F.col("principal_id").alias("x_member_id"),
    "snapshot_date",
)

group_names = dim_principal.select(
    F.col("principal_id").alias("x_group_id"),
    F.col("display_name").alias("x_group_name"),
    "snapshot_date",
)

# CELL ********************

# ---------------------------------------------------------------- base fact tables

fact_workspace_role = (
    s_ws_roles.where(F.col("principal_id").isNotNull() & (F.col("principal_id") != ""))
    .select(
        "workspace_id",
        "principal_id",
        "principal_type",
        "workspace_role",
        "snapshot_date",
    )
    .withColumn("permission_level", ws_role_level_expr)
    .dropDuplicates(["workspace_id", "principal_id", "workspace_role", "snapshot_date"])
)
save_table(fact_workspace_role, GOLD, "fact_workspace_role")

fact_item_permission = (
    s_item_perms.where(F.col("principal_id").isNotNull() & (F.col("principal_id") != ""))
    .select(
        "item_id",
        "workspace_id",
        "item_type",
        "principal_id",
        "principal_type",
        "access_right",
        "snapshot_date",
    )
    .withColumn("permission_level", parse_access_right(F.col("access_right")))
    .dropDuplicates(["item_id", "principal_id", "access_right", "snapshot_date"])
)
save_table(fact_item_permission, GOLD, "fact_item_permission")

fact_rls_role_member = s_rls.select(
    "item_id", "workspace_id", "rls_role", "principal_id", "principal_upn",
    "member_type", "table_count", "snapshot_date",
)
save_table(fact_rls_role_member, GOLD, "fact_rls_role_member")

fact_onelake_role = s_ol_roles.select(
    "role_id", "item_id", "workspace_id", "role_name", "is_default", "snapshot_date"
)
save_table(fact_onelake_role, GOLD, "fact_onelake_role")

fact_onelake_rule = s_ol_rules.select(
    "role_id", "item_id", "effect", "path", "permissions", "snapshot_date"
)
save_table(fact_onelake_rule, GOLD, "fact_onelake_rule")

fact_onelake_role_member = s_ol_members.select(
    "role_id", "item_id", "principal_id", "principal_type",
    "source_type", "source_path", "snapshot_date",
)
save_table(fact_onelake_role_member, GOLD, "fact_onelake_role_member")

fact_onelake_coverage = s_ol_cov.select(
    "workspace_id", "item_id", "item_type", "access_denied",
    F.coalesce(F.col("coverage_status"), F.lit("Ok")).alias("coverage_status"),
    "role_count", "error", "snapshot_date",
)
save_table(fact_onelake_coverage, GOLD, "fact_onelake_coverage")

# CELL ********************

# ------------------------------------------------------- data security (RLS / CLS)
#
# One fact for every fine-grained data restriction in the tenant, from either
# plane, so the app can ask "is this principal restricted, and how?" once
# instead of three times:
#
#   ModelRLS    semantic model row filter        rules TMSL  x  members scanner
#   ModelCLS    semantic model hidden column     rules TMSL  x  members scanner
#   OneLakeRLS  OneLake row constraint (SQL)     rules + members both from the
#   OneLakeCLS  OneLake column constraint          dataAccessRoles payload
#
# Grain: one row per (snapshot, item, role, rule, principal). A rule with no
# members is still emitted, with a null principal - an unassigned role is a
# meaningful finding (it restricts nothing today but is one click from doing so),
# and dropping it would silently under-report the security surface.

# --- semantic model rules joined to scanner-sourced members ------------------
ms_members = s_rls.select(
    F.col("item_id"),
    F.col("rls_role").alias("role_name"),
    F.col("principal_id"),
    F.col("principal_upn"),
    F.col("member_type"),
).where(F.col("rls_role").isNotNull())

model_sec = (
    s_ms_rules.alias("r")
    .join(ms_members.alias("m"), ["item_id", "role_name"], "left")
    .select(
        F.col("r.item_id").alias("item_id"),
        F.col("r.workspace_id").alias("workspace_id"),
        F.col("r.role_name").alias("role_name"),
        F.concat(F.lit("Model"), F.col("r.rule_type")).alias("security_type"),
        F.col("r.rule_type").alias("rule_type"),
        F.lit("SemanticModel").alias("plane"),
        F.col("r.table_name").alias("scope_table"),
        F.col("r.column_name").alias("scope_column"),
        F.col("r.rule_expression").alias("rule_expression"),
        F.coalesce(F.col("r.is_dynamic"), F.lit(False)).alias("is_dynamic"),
        F.col("m.principal_id").alias("principal_id"),
        F.col("m.principal_upn").alias("principal_upn"),
        F.col("m.member_type").alias("member_type"),
    )
)

# --- OneLake constraints joined to their role members ------------------------
ol_members_named = s_ol_members.select(
    "role_id", "item_id", "principal_id", F.col("principal_type").alias("member_type")
)
ol_role_names = s_ol_roles.select("role_id", "role_name")

onelake_sec = (
    s_ol_constraints.alias("c")
    .join(ol_role_names.alias("n"), ["role_id"], "left")
    .join(ol_members_named.alias("m"), ["role_id", "item_id"], "left")
    .join(
        dim_principal.select("principal_id", "upn").alias("p"),
        F.col("m.principal_id") == F.col("p.principal_id"),
        "left",
    )
    .select(
        F.col("c.item_id").alias("item_id"),
        F.col("c.workspace_id").alias("workspace_id"),
        F.coalesce(F.col("n.role_name"), F.col("c.role_id")).alias("role_name"),
        F.concat(F.lit("OneLake"), F.col("c.constraint_type"),
                 F.lit("LS")).alias("security_type"),
        F.when(F.col("c.constraint_type") == "Row", F.lit("RLS"))
         .otherwise(F.lit("CLS")).alias("rule_type"),
        F.lit("OneLake").alias("plane"),
        F.col("c.table_path").alias("scope_table"),
        F.col("c.columns").alias("scope_column"),
        F.col("c.rule_expression").alias("rule_expression"),
        F.lit(False).alias("is_dynamic"),
        F.col("m.principal_id").alias("principal_id"),
        # The OneLake role member payload carries only an object id, so the UPN
        # has to come from the principal directory. Without it the pane shows a
        # rule that visibly applies to somebody but cannot say to whom.
        F.col("p.upn").alias("principal_upn"),
        F.col("m.member_type").alias("member_type"),
    )
)

fact_data_security = (
    model_sec.unionByName(onelake_sec)
    .withColumn("snapshot_date", F.lit(SNAP))
    .withColumn(
        "rule_summary",
        F.when(
            F.col("rule_type") == "CLS",
            F.concat_ws(
                "", F.lit("hides "), F.col("scope_column"),
                F.when(F.col("scope_table").isNotNull(),
                       F.concat(F.lit(" on "), F.col("scope_table"))).otherwise(F.lit("")),
            ),
        ).otherwise(
            F.concat_ws(
                "",
                F.when(F.col("scope_table").isNotNull(),
                       F.concat(F.col("scope_table"), F.lit(": "))).otherwise(F.lit("")),
                F.coalesce(F.col("rule_expression"), F.lit("(filter)")),
            )
        ),
    )
    .withColumn("has_member", F.col("principal_id").isNotNull())
    .dropDuplicates(
        ["item_id", "role_name", "rule_type", "scope_table", "scope_column",
         "rule_expression", "principal_id", "snapshot_date"]
    )
)
save_table(fact_data_security, GOLD, "fact_data_security")

fact_data_security_coverage = s_ms_cov.select(
    "workspace_id", "item_id",
    F.coalesce(F.col("coverage_status"), F.lit("Ok")).alias("coverage_status"),
    "role_count", "rls_rule_count", "cls_rule_count", "error", "snapshot_date",
)
save_table(fact_data_security_coverage, GOLD, "fact_data_security_coverage")

# CELL ********************

# ---------------------------------------------------------------- effective access
#
# Four contributing sources, each producing rows in a common shape:
#   A. workspace role, held directly
#   B. workspace role, held through a group
#   C. direct item grant, held directly
#   D. direct item grant, held through a group
# Plus E: OneLake data-plane roles, which restrict rather than grant item access.

item_scope = dim_item.select(
    "item_id", "workspace_id", "item_type", "item_name", "workspace_name", "snapshot_date"
)

COMMON_COLS = [
    "principal_id",
    "item_id",
    "workspace_id",
    "item_type",
    "permission_level",
    "grant_source",
    "granted_via_id",
    "granted_via_name",
    "is_via_group",
    "access_path",
]

# --- A: direct workspace role ------------------------------------------------
ws_direct = (
    fact_workspace_role.alias("r")
    .join(
        item_scope.alias("i"),
        (F.col("r.workspace_id") == F.col("i.workspace_id"))
        & (F.col("r.snapshot_date") == F.col("i.snapshot_date")),
    )
    .select(
        F.col("r.principal_id"),
        F.col("i.item_id"),
        F.col("i.workspace_id"),
        F.col("i.item_type"),
        F.col("r.permission_level"),
        F.lit("WorkspaceRole").alias("grant_source"),
        F.lit(None).cast("string").alias("granted_via_id"),
        F.lit(None).cast("string").alias("granted_via_name"),
        F.lit(False).alias("is_via_group"),
        F.concat(
            F.lit("Workspace '"), F.col("i.workspace_name"),
            F.lit("' ("), F.col("r.workspace_role"), F.lit(") -> "),
            F.col("i.item_type"), F.lit(" '"), F.col("i.item_name"), F.lit("'"),
        ).alias("access_path"),
        F.col("r.snapshot_date"),
    )
)

# --- B: workspace role inherited through a group -----------------------------
ws_via_group = (
    fact_workspace_role.alias("r")
    .join(
        expansion.alias("x"),
        (F.col("r.principal_id") == F.col("x.x_group_id"))
        & (F.col("r.snapshot_date") == F.col("x.snapshot_date")),
    )
    .join(
        group_names.alias("gn"),
        (F.col("r.principal_id") == F.col("gn.x_group_id"))
        & (F.col("r.snapshot_date") == F.col("gn.snapshot_date")),
        "left",
    )
    .join(
        item_scope.alias("i"),
        (F.col("r.workspace_id") == F.col("i.workspace_id"))
        & (F.col("r.snapshot_date") == F.col("i.snapshot_date")),
    )
    .select(
        F.col("x.x_member_id").alias("principal_id"),
        F.col("i.item_id"),
        F.col("i.workspace_id"),
        F.col("i.item_type"),
        F.col("r.permission_level"),
        F.lit("WorkspaceRole").alias("grant_source"),
        F.col("r.principal_id").alias("granted_via_id"),
        F.col("gn.x_group_name").alias("granted_via_name"),
        F.lit(True).alias("is_via_group"),
        F.concat(
            F.lit("Group '"), F.coalesce(F.col("gn.x_group_name"), F.lit("(group)")),
            F.lit("' -> Workspace '"), F.col("i.workspace_name"),
            F.lit("' ("), F.col("r.workspace_role"), F.lit(") -> "),
            F.col("i.item_type"), F.lit(" '"), F.col("i.item_name"), F.lit("'"),
        ).alias("access_path"),
        F.col("r.snapshot_date"),
    )
)

# --- C: direct item grant ----------------------------------------------------
item_direct = (
    fact_item_permission.alias("p")
    .join(
        item_scope.alias("i"),
        (F.col("p.item_id") == F.col("i.item_id"))
        & (F.col("p.snapshot_date") == F.col("i.snapshot_date")),
    )
    .select(
        F.col("p.principal_id"),
        F.col("i.item_id"),
        F.col("i.workspace_id"),
        F.col("i.item_type"),
        F.col("p.permission_level"),
        F.lit("DirectItemGrant").alias("grant_source"),
        F.lit(None).cast("string").alias("granted_via_id"),
        F.lit(None).cast("string").alias("granted_via_name"),
        F.lit(False).alias("is_via_group"),
        F.concat(
            F.lit("Direct grant ("), F.coalesce(F.col("p.access_right"), F.lit("n/a")),
            F.lit(") -> "), F.col("i.item_type"), F.lit(" '"), F.col("i.item_name"), F.lit("'"),
        ).alias("access_path"),
        F.col("p.snapshot_date"),
    )
)

# --- D: item grant inherited through a group ---------------------------------
item_via_group = (
    fact_item_permission.alias("p")
    .join(
        expansion.alias("x"),
        (F.col("p.principal_id") == F.col("x.x_group_id"))
        & (F.col("p.snapshot_date") == F.col("x.snapshot_date")),
    )
    .join(
        group_names.alias("gn"),
        (F.col("p.principal_id") == F.col("gn.x_group_id"))
        & (F.col("p.snapshot_date") == F.col("gn.snapshot_date")),
        "left",
    )
    .join(
        item_scope.alias("i"),
        (F.col("p.item_id") == F.col("i.item_id"))
        & (F.col("p.snapshot_date") == F.col("i.snapshot_date")),
    )
    .select(
        F.col("x.x_member_id").alias("principal_id"),
        F.col("i.item_id"),
        F.col("i.workspace_id"),
        F.col("i.item_type"),
        F.col("p.permission_level"),
        F.lit("DirectItemGrant").alias("grant_source"),
        F.col("p.principal_id").alias("granted_via_id"),
        F.col("gn.x_group_name").alias("granted_via_name"),
        F.lit(True).alias("is_via_group"),
        F.concat(
            F.lit("Group '"), F.coalesce(F.col("gn.x_group_name"), F.lit("(group)")),
            F.lit("' -> grant ("), F.coalesce(F.col("p.access_right"), F.lit("n/a")),
            F.lit(") -> "), F.col("i.item_type"), F.lit(" '"), F.col("i.item_name"), F.lit("'"),
        ).alias("access_path"),
        F.col("p.snapshot_date"),
    )
)

# --- E: OneLake data-plane roles ---------------------------------------------
ol_direct = (
    fact_onelake_role_member.alias("m")
    .where(F.col("m.principal_id").isNotNull() & (F.col("m.principal_id") != ""))
    .join(
        fact_onelake_role.alias("r"),
        (F.col("m.role_id") == F.col("r.role_id"))
        & (F.col("m.snapshot_date") == F.col("r.snapshot_date")),
    )
    .join(
        item_scope.alias("i"),
        (F.col("r.item_id") == F.col("i.item_id"))
        & (F.col("r.snapshot_date") == F.col("i.snapshot_date")),
    )
    .select(
        F.col("m.principal_id"),
        F.col("i.item_id"),
        F.col("i.workspace_id"),
        F.col("i.item_type"),
        F.lit(1).alias("permission_level"),
        F.lit("OneLakeRole").alias("grant_source"),
        F.col("r.role_id").alias("granted_via_id"),
        F.col("r.role_name").alias("granted_via_name"),
        F.lit(False).alias("is_via_group"),
        F.concat(
            F.lit("OneLake role '"), F.col("r.role_name"),
            F.lit("' -> "), F.col("i.item_type"), F.lit(" '"), F.col("i.item_name"), F.lit("'"),
        ).alias("access_path"),
        F.col("m.snapshot_date"),
    )
)

# OneLake roles can also be granted to groups.
ol_via_group = (
    ol_direct.alias("o")
    .join(
        expansion.alias("x"),
        (F.col("o.principal_id") == F.col("x.x_group_id"))
        & (F.col("o.snapshot_date") == F.col("x.snapshot_date")),
    )
    .join(
        group_names.alias("gn"),
        (F.col("o.principal_id") == F.col("gn.x_group_id"))
        & (F.col("o.snapshot_date") == F.col("gn.snapshot_date")),
        "left",
    )
    .select(
        F.col("x.x_member_id").alias("principal_id"),
        F.col("o.item_id"),
        F.col("o.workspace_id"),
        F.col("o.item_type"),
        F.col("o.permission_level"),
        F.col("o.grant_source"),
        F.col("o.granted_via_id"),
        F.col("o.granted_via_name"),
        F.lit(True).alias("is_via_group"),
        F.concat(F.lit("Group '"), F.coalesce(F.col("gn.x_group_name"), F.lit("(group)")),
                 F.lit("' -> "), F.col("o.access_path")).alias("access_path"),
        F.col("o.snapshot_date"),
    )
)

all_paths = (
    ws_direct.select(*COMMON_COLS, "snapshot_date")
    .unionByName(ws_via_group.select(*COMMON_COLS, "snapshot_date"))
    .unionByName(item_direct.select(*COMMON_COLS, "snapshot_date"))
    .unionByName(item_via_group.select(*COMMON_COLS, "snapshot_date"))
    .unionByName(ol_direct.select(*COMMON_COLS, "snapshot_date"))
    .unionByName(ol_via_group.select(*COMMON_COLS, "snapshot_date"))
    .where(F.col("principal_id").isNotNull() & (F.col("principal_id") != ""))
)

# CELL ********************

# Data-plane overlay: an item with non-default OneLake roles means item-level
# permission is not the whole story, so flag those paths for the UI.
ol_restricted = (
    fact_onelake_role.where(~F.col("is_default"))
    .select("item_id", "snapshot_date")
    .distinct()
    .withColumn("data_plane_restricted", F.lit(True))
)

principal_facts = dim_principal.select(
    "principal_id", "principal_type", "is_guest", "is_orphaned",
    "account_enabled", "is_broad_group", "snapshot_date",
)

# Fine-grained restrictions that apply to this specific principal on this
# specific item. This is what turns "Ivana can read the model" into "Ivana can
# read the model, filtered to AMER, with the credit card column hidden".
principal_data_sec = (
    fact_data_security.where(F.col("principal_id").isNotNull())
    .groupBy("item_id", "principal_id")
    .agg(
        F.sum(F.when(F.col("rule_type") == "RLS", 1).otherwise(0)).alias("rls_rule_count"),
        F.sum(F.when(F.col("rule_type") == "CLS", 1).otherwise(0)).alias("cls_rule_count"),
        F.concat_ws(";", F.collect_set("role_name")).alias("data_security_roles"),
    )
    .withColumn("snapshot_date", F.lit(SNAP))
)

fact_effective_access = (
    all_paths.join(ol_restricted, ["item_id", "snapshot_date"], "left")
    .join(principal_facts, ["principal_id", "snapshot_date"], "left")
    .join(principal_data_sec, ["item_id", "principal_id", "snapshot_date"], "left")
    .withColumn("data_plane_restricted", F.coalesce(F.col("data_plane_restricted"), F.lit(False)))
    .withColumn("rls_rule_count", F.coalesce(F.col("rls_rule_count"), F.lit(0)))
    .withColumn("cls_rule_count", F.coalesce(F.col("cls_rule_count"), F.lit(0)))
    .withColumn("has_rls", F.col("rls_rule_count") > 0)
    .withColumn("has_cls", F.col("cls_rule_count") > 0)
    .withColumn("permission_name", level_to_name(F.col("permission_level")))
    .withColumn("is_direct", ~F.col("is_via_group"))
    .withColumn(
        "risk_flags",
        F.concat_ws(
            ";",
            F.when(F.col("is_guest"), F.lit("GuestAccess")),
            F.when(F.col("is_orphaned"), F.lit("OrphanedPrincipal")),
            F.when(
                (F.col("principal_type") == "ServicePrincipal") & (F.col("permission_level") >= 3),
                F.lit("ServicePrincipalWriteAccess"),
            ),
            # Reshare is inherent to workspace Admin/Member, so only an explicit
            # item-level reshare grant is noteworthy on its own.
            F.when(
                (F.col("grant_source") == "DirectItemGrant") & (F.col("permission_level") >= 4),
                F.lit("ItemResharePrivilege"),
            ),
            F.when(F.col("is_broad_group"), F.lit("BroadGroupGrant")),
            F.when(
                F.col("is_via_group") & F.col("data_plane_restricted"),
                F.lit("GroupGrantOnSecuredData"),
            ),
            # Write or higher bypasses row filters entirely - the RLS role is
            # decorative for this principal, which reliably surprises admins.
            F.when(
                (F.col("permission_level") >= 3) & (F.col("rls_rule_count") > 0),
                F.lit("RlsBypassedByWriteAccess"),
            ),
        ),
    )
    .withColumn("is_risk", F.length(F.col("risk_flags")) > 0)
    .select(
        "snapshot_date",
        "principal_id",
        "item_id",
        "workspace_id",
        "item_type",
        "grant_source",
        "granted_via_id",
        "granted_via_name",
        "is_via_group",
        "is_direct",
        "permission_level",
        "permission_name",
        "access_path",
        "data_plane_restricted",
        "has_rls",
        "has_cls",
        "rls_rule_count",
        "cls_rule_count",
        "data_security_roles",
        "risk_flags",
        "is_risk",
    )
    .dropDuplicates(
        ["snapshot_date", "principal_id", "item_id", "grant_source", "granted_via_id", "permission_level"]
    )
)

save_table(fact_effective_access, GOLD, "fact_effective_access")

# CELL ********************

# ---------------------------------------------------------------- summaries
#
# One row per principal/item with the strongest permission and the number of
# distinct routes - keeps the common "can X reach Y?" query cheap.

w = Window.partitionBy("snapshot_date", "principal_id", "item_id").orderBy(
    F.col("permission_level").desc(), F.col("is_direct").desc()
)

fact_access_summary = (
    fact_effective_access.withColumn("rn", F.row_number().over(w))
    .withColumn("path_count", F.count("*").over(Window.partitionBy("snapshot_date", "principal_id", "item_id")))
    .where(F.col("rn") == 1)
    .drop("rn")
    .withColumnRenamed("permission_level", "max_permission_level")
    .withColumnRenamed("permission_name", "max_permission_name")
    .withColumnRenamed("access_path", "primary_access_path")
)
save_table(fact_access_summary, GOLD, "fact_access_summary")

# CELL ********************

# ---------------------------------------------------------------- dim_date

existing = (
    spark.table(f"{GOLD}.fact_effective_access")
    .select("snapshot_date")
    .distinct()
)
dim_date = (
    existing.withColumn("date_key", F.to_date("snapshot_date"))
    .withColumn("year", F.year("date_key"))
    .withColumn("month", F.month("date_key"))
    .withColumn("day", F.dayofmonth("date_key"))
    .withColumn("month_name", F.date_format("date_key", "MMM yyyy"))
    .withColumn("is_latest", F.col("snapshot_date") == F.lit(SNAP))
)
save_table(dim_date, GOLD, "dim_date", partition_by=None)

# CELL ********************

total = spark.table(f"{GOLD}.fact_effective_access").where(F.col("snapshot_date") == SNAP).count()
principals_ct = fact_access_summary.select("principal_id").distinct().count()
items_ct = fact_access_summary.select("item_id").distinct().count()

log_run(
    "build_gold",
    "Succeeded",
    total,
    f"{principals_ct} principals x {items_ct} items, {total} access paths",
)
print(f"[onesafe] gold complete: {total} access paths, {principals_ct} principals, {items_ct} items")
