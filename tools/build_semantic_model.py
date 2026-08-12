"""Build and deploy the OneSafe Direct Lake semantic model.

Generates a TMSL model over the ``gold`` schema of the OneSafe lakehouse and
deploys it through the Fabric semantic model definition API.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from onesafe_config import load as _load_config, load_notebook_ids as _load_notebook_ids
CFG = _load_config()

WORKSPACE_ID = CFG["workspaceId"]
LAKEHOUSE_ID = CFG["lakehouseId"]
SQL_ENDPOINT = CFG["sqlEndpoint"]
# The M database argument is the SQL analytics endpoint id, not the lakehouse id.
SQL_ENDPOINT_ID = CFG["sqlEndpointId"]
MODEL_NAME = "sm_onesafe"
FABRIC_API = "https://api.fabric.microsoft.com"


# --------------------------------------------------------------------------- API

def token() -> str:
    return subprocess.run(
        ["az", "account", "get-access-token", "--resource", FABRIC_API,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    ).stdout.strip()


def request(method: str, url: str, body=None, tok: str = "") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            out = json.loads(raw) if raw else {}
            if not isinstance(out, dict):
                out = {"value": out}
            if resp.status == 202:
                out["_location"] = resp.headers.get("Location", "")
            return out
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{exc.code} {method} {url}: {exc.read().decode()[:900]}") from None


def await_operation(location: str, tok: str, timeout_s: int = 900) -> dict:
    if not location:
        return {}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        state = request("GET", location, tok=tok)
        if state.get("status") == "Succeeded":
            try:
                return request("GET", location.rstrip("/") + "/result", tok=tok) or {}
            except RuntimeError:
                return {}
        if state.get("status") == "Failed":
            raise RuntimeError(f"Operation failed: {json.dumps(state)[:700]}")
    raise TimeoutError(location)


# ------------------------------------------------------------------- TMSL model

def tag() -> str:
    return str(uuid.uuid4())


def col(name: str, dtype: str, *, hidden: bool = False, fmt: str | None = None,
        sort_by: str | None = None, desc: str | None = None) -> dict:
    c: dict = {
        "name": name,
        "dataType": dtype,
        "sourceColumn": name,
        "lineageTag": tag(),
        "summarizeBy": "none",
    }
    if hidden:
        c["isHidden"] = True
    if fmt:
        c["formatString"] = fmt
    if sort_by:
        c["sortByColumn"] = sort_by
    if desc:
        c["description"] = desc
    return c


def table(name: str, columns: list[dict], *, measures: list[dict] | None = None,
          hidden: bool = False, hierarchies: list[dict] | None = None,
          desc: str | None = None) -> dict:
    t: dict = {
        "name": name,
        "lineageTag": tag(),
        "columns": columns,
        "partitions": [
            {
                "name": f"{name}-partition",
                "mode": "directLake",
                "source": {
                    "type": "entity",
                    "entityName": name,
                    "schemaName": "gold",
                    "expressionSource": "DatabaseQuery",
                },
            }
        ],
    }
    if desc:
        t["description"] = desc
    if measures:
        t["measures"] = measures
    if hierarchies:
        t["hierarchies"] = hierarchies
    if hidden:
        t["isHidden"] = True
    return t


def measure(name: str, expr: str, *, fmt: str | None = None, folder: str | None = None,
            desc: str | None = None) -> dict:
    m: dict = {"name": name, "expression": expr, "lineageTag": tag()}
    if fmt:
        m["formatString"] = fmt
    if folder:
        m["displayFolder"] = folder
    if desc:
        m["description"] = desc
    return m


def rel(from_table: str, from_col: str, to_table: str, to_col: str,
        *, cross: str | None = None, active: bool = True) -> dict:
    r: dict = {
        "name": tag(),
        "fromTable": from_table,
        "fromColumn": from_col,
        "toTable": to_table,
        "toColumn": to_col,
    }
    if cross:
        r["crossFilteringBehavior"] = cross
    if not active:
        r["isActive"] = False
    return r


INT = "int64"
STR = "string"
BOOL = "boolean"
DT = "dateTime"
DEC = "decimal"

WHOLE = "#,0"
PCT = "0.0%"


def build_model() -> dict:
    # ---------------------------------------------------------------- dimensions
    dim_principal = table(
        "dim_principal",
        [
            col("principal_id", STR, hidden=True),
            col("display_name", STR, desc="Display name, or '(unknown principal)' when Entra cannot resolve it."),
            col("upn", STR),
            col("principal_type", STR),
            col("account_enabled", BOOL),
            col("department", STR),
            col("job_title", STR),
            col("is_guest", BOOL),
            col("app_id", STR),
            col("is_resolved", BOOL),
            col("group_member_count", INT, fmt=WHOLE),
            col("is_broad_group", BOOL),
            col("is_orphaned", BOOL, desc="Principal no longer resolves in Entra, or is disabled, yet still holds access."),
            col("first_seen_date", STR),
            col("last_seen_date", STR),
        ],
        desc="Every user, group, service principal or managed identity that holds access.",
    )

    dim_workspace = table(
        "dim_workspace",
        [
            col("workspace_id", STR, hidden=True),
            col("workspace_name", STR),
            col("workspace_type", STR),
            col("state", STR),
            col("is_personal", BOOL),
            col("capacity_id", STR, hidden=True),
            col("capacity_name", STR),
            col("capacity_sku", STR),
            col("first_seen_date", STR),
            col("last_seen_date", STR),
        ],
        hierarchies=[
            {
                "name": "Capacity hierarchy",
                "lineageTag": tag(),
                "levels": [
                    {"name": "Capacity", "ordinal": 0, "column": "capacity_name", "lineageTag": tag()},
                    {"name": "Workspace", "ordinal": 1, "column": "workspace_name", "lineageTag": tag()},
                ],
            }
        ],
    )

    dim_item = table(
        "dim_item",
        [
            col("item_id", STR, hidden=True),
            col("workspace_id", STR, hidden=True),
            col("workspace_name", STR),
            col("item_type", STR),
            col("item_name", STR),
            col("description", STR),
            col("created_by", STR),
            col("created_by_id", STR, hidden=True),
            col("created_date", STR),
            col("modified_date", STR),
            col("endorsement", STR),
            col("sensitivity_label_id", STR),
            col("is_personal_workspace", BOOL),
            col("onelake_role_count", INT, fmt=WHOLE),
            col("has_onelake_security", BOOL),
            col("rls_rule_count", INT, fmt=WHOLE),
            col("cls_rule_count", INT, fmt=WHOLE),
            col("has_rls", BOOL, desc="At least one row-level rule is defined on this item."),
            col("has_cls", BOOL, desc="At least one column is hidden by a security rule."),
            col("has_dynamic_rls", BOOL,
                desc="A filter resolves per signed-in user rather than to a fixed value."),
            col("first_seen_date", STR),
            col("last_seen_date", STR),
        ],
        hierarchies=[
            {
                "name": "Item hierarchy",
                "lineageTag": tag(),
                "levels": [
                    {"name": "Workspace", "ordinal": 0, "column": "workspace_name", "lineageTag": tag()},
                    {"name": "Item type", "ordinal": 1, "column": "item_type", "lineageTag": tag()},
                    {"name": "Item", "ordinal": 2, "column": "item_name", "lineageTag": tag()},
                ],
            }
        ],
    )

    dim_capacity = table(
        "dim_capacity",
        [
            col("capacity_id", STR, hidden=True),
            col("capacity_name", STR),
            col("sku", STR),
            col("region", STR),
            col("state", STR),
            col("first_seen_date", STR),
            col("last_seen_date", STR),
        ],
    )

    dim_date = table(
        "dim_date",
        [
            col("snapshot_date", STR),
            col("date_key", DT, fmt="yyyy-mm-dd"),
            col("year", INT, fmt="0"),
            col("month", INT, fmt="0"),
            col("day", INT, fmt="0"),
            col("month_name", STR),
            col("is_latest", BOOL, desc="Marks the most recent successful snapshot."),
        ],
        desc="Snapshot calendar. Filter on is_latest = TRUE for current-state analysis.",
    )

    # -------------------------------------------------------------------- facts
    eff_measures = [
        measure("Access Paths", "COUNTROWS('fact_effective_access')", fmt=WHOLE,
                folder="01 Access",
                desc="Every distinct route by which a principal reaches an item."),
        measure("Principals with Access",
                "DISTINCTCOUNT('fact_effective_access'[principal_id])", fmt=WHOLE, folder="01 Access"),
        measure("Items Accessible",
                "DISTINCTCOUNT('fact_effective_access'[item_id])", fmt=WHOLE, folder="01 Access"),
        measure("Workspaces Accessible",
                "DISTINCTCOUNT('fact_effective_access'[workspace_id])", fmt=WHOLE, folder="01 Access"),
        measure("Direct Access Paths",
                "CALCULATE([Access Paths], 'fact_effective_access'[is_direct] = TRUE())",
                fmt=WHOLE, folder="01 Access"),
        measure("Inherited Access Paths",
                "CALCULATE([Access Paths], 'fact_effective_access'[is_via_group] = TRUE())",
                fmt=WHOLE, folder="01 Access"),
        measure("Direct Access %",
                "DIVIDE([Direct Access Paths], [Access Paths])", fmt=PCT, folder="01 Access"),
        measure("Inherited Access %",
                "DIVIDE([Inherited Access Paths], [Access Paths])", fmt=PCT, folder="01 Access"),
        measure("Max Permission Level",
                "MAX('fact_effective_access'[permission_level])", fmt="0", folder="01 Access"),
        measure("Avg Paths per Principal",
                "DIVIDE([Access Paths], [Principals with Access])", fmt="#,0.0", folder="01 Access"),

        measure("Risk Paths",
                "CALCULATE([Access Paths], 'fact_effective_access'[is_risk] = TRUE())",
                fmt=WHOLE, folder="02 Risk"),
        measure("Risk Path %", "DIVIDE([Risk Paths], [Access Paths])", fmt=PCT, folder="02 Risk"),
        measure("Guest Access Paths",
                "CALCULATE([Access Paths], CONTAINSSTRING('fact_effective_access'[risk_flags], \"GuestAccess\"))",
                fmt=WHOLE, folder="02 Risk"),
        measure("Orphaned Access Paths",
                "CALCULATE([Access Paths], CONTAINSSTRING('fact_effective_access'[risk_flags], \"OrphanedPrincipal\"))",
                fmt=WHOLE, folder="02 Risk",
                desc="Access still held by disabled or deleted principals."),
        measure("Service Principal Write Paths",
                "CALCULATE([Access Paths], CONTAINSSTRING('fact_effective_access'[risk_flags], \"ServicePrincipalWriteAccess\"))",
                fmt=WHOLE, folder="02 Risk"),
        measure("Item Reshare Paths",
                "CALCULATE([Access Paths], CONTAINSSTRING('fact_effective_access'[risk_flags], \"ItemResharePrivilege\"))",
                fmt=WHOLE, folder="02 Risk"),
        measure("Broad Group Paths",
                "CALCULATE([Access Paths], CONTAINSSTRING('fact_effective_access'[risk_flags], \"BroadGroupGrant\"))",
                fmt=WHOLE, folder="02 Risk"),
        measure("Group Grants on Secured Data",
                "CALCULATE([Access Paths], CONTAINSSTRING('fact_effective_access'[risk_flags], \"GroupGrantOnSecuredData\"))",
                fmt=WHOLE, folder="02 Risk"),
        measure("Risky Principals",
                "CALCULATE([Principals with Access], 'fact_effective_access'[is_risk] = TRUE())",
                fmt=WHOLE, folder="02 Risk"),
        measure(
            "Over-Privileged Score",
            "VAR HighPerm = CALCULATE([Access Paths], 'fact_effective_access'[permission_level] >= 4)\n"
            "VAR Total = [Access Paths]\n"
            "RETURN DIVIDE(HighPerm, Total)",
            fmt=PCT, folder="02 Risk",
            desc="Share of access paths conferring reshare or admin rights.",
        ),

        measure("Data-Plane Restricted Paths",
                "CALCULATE([Access Paths], 'fact_effective_access'[data_plane_restricted] = TRUE())",
                fmt=WHOLE, folder="03 OneLake",
                desc="Paths to items whose data access is further narrowed by OneLake Security roles."),
        measure("Data-Plane Restricted %",
                "DIVIDE([Data-Plane Restricted Paths], [Access Paths])", fmt=PCT, folder="03 OneLake"),
        measure("RLS Restricted Paths",
                "CALCULATE([Access Paths], 'fact_effective_access'[has_rls] = TRUE())",
                fmt=WHOLE, folder="08 Data Security",
                desc="Access paths where a row-level security rule applies to this principal."),
        measure("CLS Restricted Paths",
                "CALCULATE([Access Paths], 'fact_effective_access'[has_cls] = TRUE())",
                fmt=WHOLE, folder="08 Data Security",
                desc="Access paths where a column is hidden from this principal."),
        measure("Row or Column Restricted Paths",
                "CALCULATE([Access Paths], "
                "FILTER('fact_effective_access', "
                "'fact_effective_access'[has_rls] || 'fact_effective_access'[has_cls]))",
                fmt=WHOLE, folder="08 Data Security"),
        measure("Unrestricted Paths",
                "[Access Paths] - [Row or Column Restricted Paths]",
                fmt=WHOLE, folder="08 Data Security",
                desc="Paths with no row or column restriction at all."),
        measure("RLS Bypassed by Write Paths",
                "CALCULATE([Access Paths], "
                "CONTAINSSTRING('fact_effective_access'[risk_flags], \"RlsBypassedByWriteAccess\"))",
                fmt=WHOLE, folder="02 Risk",
                desc="Write or higher on an item whose row filters therefore do not apply to them."),
    ]

    fact_effective_access = table(
        "fact_effective_access",
        [
            col("snapshot_date", STR, hidden=True),
            col("principal_id", STR, hidden=True),
            col("item_id", STR, hidden=True),
            col("workspace_id", STR, hidden=True),
            col("item_type", STR),
            col("grant_source", STR, desc="WorkspaceRole, DirectItemGrant or OneLakeRole."),
            col("granted_via_id", STR, hidden=True),
            col("granted_via_name", STR, desc="The group or OneLake role the access flows through."),
            col("is_via_group", BOOL),
            col("is_direct", BOOL),
            col("permission_level", INT, fmt="0",
                desc="0 None, 1 Read, 2 Build/Explore, 3 Write, 4 Reshare, 5 Admin."),
            col("permission_name", STR, sort_by="permission_level"),
            col("access_path", STR, desc="Human-readable chain from grant to item."),
            col("data_plane_restricted", BOOL),
            col("has_rls", BOOL, desc="A row-level rule narrows what this principal sees."),
            col("has_cls", BOOL, desc="A column is hidden from this principal."),
            col("rls_rule_count", INT, fmt=WHOLE),
            col("cls_rule_count", INT, fmt=WHOLE),
            col("data_security_roles", STR,
                desc="Semicolon-separated RLS/CLS roles this principal holds on the item."),
            col("risk_flags", STR),
            col("is_risk", BOOL),
        ],
        measures=eff_measures,
        desc="One row per access path. The grain deliberately preserves every route to an item.",
    )

    fact_access_summary = table(
        "fact_access_summary",
        [
            col("snapshot_date", STR, hidden=True),
            col("principal_id", STR, hidden=True),
            col("item_id", STR, hidden=True),
            col("workspace_id", STR, hidden=True),
            col("item_type", STR),
            col("grant_source", STR),
            col("granted_via_id", STR, hidden=True),
            col("granted_via_name", STR),
            col("is_via_group", BOOL),
            col("is_direct", BOOL),
            col("max_permission_level", INT, fmt="0"),
            col("max_permission_name", STR, sort_by="max_permission_level"),
            col("primary_access_path", STR),
            col("data_plane_restricted", BOOL),
            col("risk_flags", STR),
            col("is_risk", BOOL),
            col("path_count", INT, fmt=WHOLE),
        ],
        measures=[
            measure("Principal-Item Pairs", "COUNTROWS('fact_access_summary')", fmt=WHOLE,
                    folder="01 Access",
                    desc="Distinct principal/item combinations, collapsing multiple routes."),
            measure("Multi-Path Pairs",
                    "CALCULATE([Principal-Item Pairs], 'fact_access_summary'[path_count] > 1)",
                    fmt=WHOLE, folder="01 Access"),
            measure("Multi-Path %", "DIVIDE([Multi-Path Pairs], [Principal-Item Pairs])",
                    fmt=PCT, folder="01 Access"),
        ],
        desc="Strongest permission per principal and item, with the number of contributing routes.",
    )

    fact_workspace_role = table(
        "fact_workspace_role",
        [
            col("workspace_id", STR, hidden=True),
            col("principal_id", STR, hidden=True),
            col("principal_type", STR),
            col("workspace_role", STR),
            col("permission_level", INT, fmt="0"),
            col("snapshot_date", STR, hidden=True),
        ],
        measures=[
            measure("Workspace Role Assignments", "COUNTROWS('fact_workspace_role')",
                    fmt=WHOLE, folder="04 Grants"),
            measure("Workspace Admins",
                    "CALCULATE([Workspace Role Assignments], 'fact_workspace_role'[workspace_role] = \"Admin\")",
                    fmt=WHOLE, folder="04 Grants"),
        ],
    )

    fact_item_permission = table(
        "fact_item_permission",
        [
            col("item_id", STR, hidden=True),
            col("workspace_id", STR, hidden=True),
            col("item_type", STR),
            col("principal_id", STR, hidden=True),
            col("principal_type", STR),
            col("access_right", STR),
            col("permission_level", INT, fmt="0"),
            col("snapshot_date", STR, hidden=True),
        ],
        measures=[
            measure("Item Grants", "COUNTROWS('fact_item_permission')", fmt=WHOLE, folder="04 Grants"),
            measure("Build Grants",
                    "CALCULATE([Item Grants], CONTAINSSTRING('fact_item_permission'[access_right], \"Build\"))",
                    fmt=WHOLE, folder="04 Grants",
                    desc="Semantic model Build rights - the permission that allows new reports over a model."),
            measure("Reshare Grants",
                    "CALCULATE([Item Grants], CONTAINSSTRING('fact_item_permission'[access_right], \"Reshare\"))",
                    fmt=WHOLE, folder="04 Grants"),
        ],
    )

    fact_rls_role_member = table(
        "fact_rls_role_member",
        [
            col("item_id", STR, hidden=True),
            col("workspace_id", STR, hidden=True),
            col("rls_role", STR),
            col("principal_id", STR, hidden=True),
            col("principal_upn", STR),
            col("member_type", STR),
            col("table_count", INT, fmt=WHOLE),
            col("snapshot_date", STR, hidden=True),
        ],
        measures=[
            measure("RLS Memberships", "COUNTROWS('fact_rls_role_member')", fmt=WHOLE, folder="05 Model security"),
            measure("Models with RLS",
                    "DISTINCTCOUNT('fact_rls_role_member'[item_id])", fmt=WHOLE, folder="05 Model security"),
        ],
    )

    fact_onelake_role = table(
        "fact_onelake_role",
        [
            col("role_id", STR, hidden=True),
            col("item_id", STR, hidden=True),
            col("workspace_id", STR, hidden=True),
            col("role_name", STR),
            col("is_default", BOOL),
            col("snapshot_date", STR, hidden=True),
        ],
        measures=[
            measure("OneLake Roles", "COUNTROWS('fact_onelake_role')", fmt=WHOLE, folder="03 OneLake"),
            measure("Custom OneLake Roles",
                    "CALCULATE([OneLake Roles], 'fact_onelake_role'[is_default] = FALSE())",
                    fmt=WHOLE, folder="03 OneLake"),
            measure("Items with OneLake Security",
                    "DISTINCTCOUNT('fact_onelake_role'[item_id])", fmt=WHOLE, folder="03 OneLake"),
        ],
    )

    fact_onelake_role_member = table(
        "fact_onelake_role_member",
        [
            col("role_id", STR, hidden=True),
            col("item_id", STR, hidden=True),
            col("principal_id", STR, hidden=True),
            col("principal_type", STR),
            col("source_type", STR),
            col("source_path", STR),
            col("snapshot_date", STR, hidden=True),
        ],
        measures=[
            measure("OneLake Role Members", "COUNTROWS('fact_onelake_role_member')",
                    fmt=WHOLE, folder="03 OneLake"),
        ],
    )

    fact_onelake_rule = table(
        "fact_onelake_rule",
        [
            col("role_id", STR, hidden=True),
            col("item_id", STR, hidden=True),
            col("effect", STR),
            col("path", STR),
            col("permissions", STR),
            col("snapshot_date", STR, hidden=True),
        ],
        measures=[
            measure("OneLake Rules", "COUNTROWS('fact_onelake_rule')", fmt=WHOLE, folder="03 OneLake"),
        ],
    )

    fact_onelake_coverage = table(
        "fact_onelake_coverage",
        [
            col("workspace_id", STR, hidden=True),
            col("item_id", STR, hidden=True),
            col("item_type", STR),
            col("access_denied", BOOL),
            col("coverage_status", STR,
                desc="Ok | FeatureDisabled | NotSupported | AccessDenied | Error. "
                     "Distinguishes an item with no roles from one where OneLake "
                     "Security is not switched on at all."),
            col("role_count", INT, fmt=WHOLE),
            col("error", STR),
            col("snapshot_date", STR, hidden=True),
        ],
        measures=[
            measure("OneLake Items Scanned", "COUNTROWS('fact_onelake_coverage')",
                    fmt=WHOLE, folder="03 OneLake"),
            measure("OneLake Scan Gaps",
                    "CALCULATE([OneLake Items Scanned], 'fact_onelake_coverage'[access_denied] = TRUE())",
                    fmt=WHOLE, folder="03 OneLake",
                    desc="Items the scanner could not read - coverage gaps are surfaced, never hidden."),
            measure("OneLake Feature Disabled Items",
                    "CALCULATE([OneLake Items Scanned], "
                    "'fact_onelake_coverage'[coverage_status] = \"FeatureDisabled\")",
                    fmt=WHOLE, folder="03 OneLake",
                    desc="OneLake-capable items where OneLake Security has never been "
                         "enabled, so no data-plane scoping is possible."),
            measure("OneLake Coverage %",
                    "DIVIDE([OneLake Items Scanned] - [OneLake Scan Gaps], [OneLake Items Scanned])",
                    fmt=PCT, folder="03 OneLake"),
        ],
    )

    fact_data_security = table(
        "fact_data_security",
        [
            col("snapshot_date", STR, hidden=True),
            col("item_id", STR, hidden=True),
            col("workspace_id", STR, hidden=True),
            col("principal_id", STR, hidden=True),
            col("principal_upn", STR),
            col("member_type", STR),
            col("role_name", STR, desc="The RLS/CLS role or OneLake data access role."),
            col("security_type", STR,
                desc="ModelRLS | ModelCLS | OneLakeRowLS | OneLakeColumnLS."),
            col("rule_type", STR, desc="RLS or CLS."),
            col("plane", STR, desc="SemanticModel or OneLake."),
            col("scope_table", STR, desc="Table or OneLake table path the rule applies to."),
            col("scope_column", STR, desc="Column(s) the rule applies to, for CLS."),
            col("rule_expression", STR, desc="The DAX filter or SQL predicate, verbatim."),
            col("rule_summary", STR, desc="One-line human-readable form of the rule."),
            col("is_dynamic", BOOL,
                desc="The filter resolves per signed-in user (USERPRINCIPALNAME / CUSTOMDATA)."),
            col("has_member", BOOL,
                desc="False means the rule exists but nobody is assigned to it today."),
        ],
        measures=[
            measure("Data Security Rules", "COUNTROWS('fact_data_security')",
                    fmt=WHOLE, folder="08 Data Security",
                    desc="Row- and column-level rules, counted per assigned principal."),
            measure("RLS Rules",
                    "CALCULATE([Data Security Rules], 'fact_data_security'[rule_type] = \"RLS\")",
                    fmt=WHOLE, folder="08 Data Security"),
            measure("CLS Rules",
                    "CALCULATE([Data Security Rules], 'fact_data_security'[rule_type] = \"CLS\")",
                    fmt=WHOLE, folder="08 Data Security"),
            measure("Model RLS/CLS Rules",
                    "CALCULATE([Data Security Rules], 'fact_data_security'[plane] = \"SemanticModel\")",
                    fmt=WHOLE, folder="08 Data Security"),
            measure("OneLake RLS/CLS Rules",
                    "CALCULATE([Data Security Rules], 'fact_data_security'[plane] = \"OneLake\")",
                    fmt=WHOLE, folder="08 Data Security"),
            measure("Data Security Roles",
                    "CALCULATE(DISTINCTCOUNT('fact_data_security'[role_name]))",
                    fmt=WHOLE, folder="08 Data Security"),
            measure("Items with RLS or CLS",
                    "CALCULATE(DISTINCTCOUNT('fact_data_security'[item_id]))",
                    fmt=WHOLE, folder="08 Data Security"),
            measure("Principals under RLS or CLS",
                    "CALCULATE(DISTINCTCOUNT('fact_data_security'[principal_id]))",
                    fmt=WHOLE, folder="08 Data Security"),
            measure("Dynamic RLS Rules",
                    "CALCULATE([Data Security Rules], 'fact_data_security'[is_dynamic] = TRUE())",
                    fmt=WHOLE, folder="08 Data Security",
                    desc="Filters evaluated against the signed-in user rather than a fixed value."),
            measure("Unassigned Data Security Rules",
                    "CALCULATE([Data Security Rules], 'fact_data_security'[has_member] = FALSE())",
                    fmt=WHOLE, folder="08 Data Security",
                    desc="Rules defined but with no member - they restrict nobody today."),
        ],
        desc="Every row- and column-level restriction in the tenant, from both the "
             "semantic model and the OneLake plane, joined to the principals it applies to.",
    )

    fact_data_security_coverage = table(
        "fact_data_security_coverage",
        [
            col("workspace_id", STR, hidden=True),
            col("item_id", STR, hidden=True),
            col("coverage_status", STR,
                desc="Ok | NotSupported | AccessDenied | Error. NotSupported covers "
                     "models with no readable definition, such as push or streaming datasets."),
            col("role_count", INT, fmt=WHOLE),
            col("rls_rule_count", INT, fmt=WHOLE),
            col("cls_rule_count", INT, fmt=WHOLE),
            col("error", STR),
            col("snapshot_date", STR, hidden=True),
        ],
        measures=[
            measure("Models Inspected", "COUNTROWS('fact_data_security_coverage')",
                    fmt=WHOLE, folder="08 Data Security"),
            measure("Model Security Read Gaps",
                    "CALCULATE([Models Inspected], "
                    "'fact_data_security_coverage'[coverage_status] IN {\"AccessDenied\", \"Error\"})",
                    fmt=WHOLE, folder="08 Data Security",
                    desc="Models whose security could not be read. Unknown, never assumed clear."),
            measure("Models with RLS Defined",
                    "CALCULATE([Models Inspected], 'fact_data_security_coverage'[rls_rule_count] > 0)",
                    fmt=WHOLE, folder="08 Data Security",
                    desc="Models with at least one row filter defined, whether or not "
                         "anybody is assigned to the role."),
            measure("Models with CLS Defined",
                    "CALCULATE([Models Inspected], 'fact_data_security_coverage'[cls_rule_count] > 0)",
                    fmt=WHOLE, folder="08 Data Security"),
            measure("Model Security Coverage %",
                    "DIVIDE([Models Inspected] - [Model Security Read Gaps], [Models Inspected])",
                    fmt=PCT, folder="08 Data Security"),
        ],
    )

    fact_access_change = table(
        "fact_access_change",
        [
            col("snapshot_date", STR, hidden=True),
            col("prev_snapshot_date", STR),
            col("principal_id", STR, hidden=True),
            col("item_id", STR, hidden=True),
            col("workspace_id", STR, hidden=True),
            col("item_type", STR),
            col("change_type", STR, desc="Added, Removed or Elevated."),
            col("prev_permission_level", INT, fmt="0"),
            col("new_permission_level", INT, fmt="0"),
            col("prev_permission_name", STR),
            col("new_permission_name", STR),
            col("access_path", STR),
        ],
        measures=[
            measure("Access Changes", "COUNTROWS('fact_access_change')", fmt=WHOLE, folder="06 Drift"),
            measure("Access Added",
                    "CALCULATE([Access Changes], 'fact_access_change'[change_type] = \"Added\")",
                    fmt=WHOLE, folder="06 Drift"),
            measure("Access Removed",
                    "CALCULATE([Access Changes], 'fact_access_change'[change_type] = \"Removed\")",
                    fmt=WHOLE, folder="06 Drift"),
            measure("Access Elevated",
                    "CALCULATE([Access Changes], 'fact_access_change'[change_type] = \"Elevated\")",
                    fmt=WHOLE, folder="06 Drift",
                    desc="Existing access whose permission level increased since the previous snapshot."),
            measure(
                "Access Added 7d",
                "CALCULATE([Access Added], FILTER(ALL('dim_date'), "
                "'dim_date'[date_key] > MAX('dim_date'[date_key]) - 7 && "
                "'dim_date'[date_key] <= MAX('dim_date'[date_key])))",
                fmt=WHOLE, folder="06 Drift",
            ),
            measure(
                "Access Removed 7d",
                "CALCULATE([Access Removed], FILTER(ALL('dim_date'), "
                "'dim_date'[date_key] > MAX('dim_date'[date_key]) - 7 && "
                "'dim_date'[date_key] <= MAX('dim_date'[date_key])))",
                fmt=WHOLE, folder="06 Drift",
            ),
            measure(
                "Access Added 30d",
                "CALCULATE([Access Added], FILTER(ALL('dim_date'), "
                "'dim_date'[date_key] > MAX('dim_date'[date_key]) - 30 && "
                "'dim_date'[date_key] <= MAX('dim_date'[date_key])))",
                fmt=WHOLE, folder="06 Drift",
            ),
        ],
    )

    bridge_group_membership = table(
        "bridge_group_membership",
        [
            col("group_id", STR, hidden=True),
            col("principal_id", STR, hidden=True),
            col("member_name", STR),
            col("member_upn", STR),
            col("member_type", STR),
            col("snapshot_date", STR, hidden=True),
        ],
        measures=[
            measure("Group Memberships", "COUNTROWS('bridge_group_membership')",
                    fmt=WHOLE, folder="04 Grants"),
            measure("Expanded Groups", "DISTINCTCOUNT('bridge_group_membership'[group_id])",
                    fmt=WHOLE, folder="04 Grants"),
        ],
    )

    fact_validation = table(
        "fact_validation",
        [
            col("principal_id", STR, hidden=True),
            col("upn", STR),
            col("api_item_count", INT, fmt=WHOLE),
            col("model_item_count", INT, fmt=WHOLE),
            col("matched_count", INT, fmt=WHOLE),
            col("coverage_pct", DEC, fmt="0.0"),
            col("status", STR),
            col("snapshot_date", STR, hidden=True),
            col("snapshot_ts", STR, hidden=True),
        ],
        measures=[
            measure("Validation Samples", "COUNTROWS('fact_validation')", fmt=WHOLE, folder="07 Quality"),
            measure("Model Accuracy", "AVERAGE('fact_validation'[coverage_pct]) / 100",
                    fmt=PCT, folder="07 Quality",
                    desc="Agreement between OneSafe and the Fabric List Access Entities API."),
        ],
    )

    fact_pipeline_run = table(
        "fact_pipeline_run",
        [
            col("step", STR, sort_by="step_order"),
            col("step_order", INT, fmt="0", hidden=True),
            col("status", STR),
            col("records", INT, fmt=WHOLE),
            col("detail", STR),
            col("is_healthy", BOOL),
            col("snapshot_date", STR, hidden=True),
            col("snapshot_ts", STR, hidden=True),
        ],
        measures=[
            measure("Pipeline Steps", "COUNTROWS('fact_pipeline_run')", fmt=WHOLE,
                    folder="07 Quality"),
            measure("Pipeline Failures",
                    "CALCULATE(COUNTROWS('fact_pipeline_run'), "
                    "'fact_pipeline_run'[is_healthy] = FALSE())",
                    fmt=WHOLE, folder="07 Quality"),
            measure("Pipeline Health %",
                    "DIVIDE(CALCULATE(COUNTROWS('fact_pipeline_run'), "
                    "'fact_pipeline_run'[is_healthy] = TRUE()), "
                    "COUNTROWS('fact_pipeline_run'))",
                    fmt=PCT, folder="07 Quality",
                    desc="Share of daily pipeline steps that completed cleanly."),
            measure("Last Refresh",
                    "MAX('fact_pipeline_run'[snapshot_ts])", folder="07 Quality"),
        ],
        desc="Per-step outcome of the daily security scan, so stale or partial "
             "data is visible rather than silently assumed fresh.",
    )

    tables = [
        dim_date, dim_principal, dim_workspace, dim_item, dim_capacity,
        bridge_group_membership,
        fact_effective_access, fact_access_summary, fact_access_change,
        fact_workspace_role, fact_item_permission, fact_rls_role_member,
        fact_onelake_role, fact_onelake_role_member, fact_onelake_rule,
        fact_onelake_coverage, fact_validation, fact_pipeline_run,
        fact_data_security, fact_data_security_coverage,
    ]

    # Star schema. Dimensions hold one row per entity (latest attributes), so
    # keys are unique and every fact joins on a single column. Snapshot history
    # lives in the facts, which all relate to dim_date.
    relationships = [
        # --- date
        rel("fact_effective_access", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_access_summary", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_access_change", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_workspace_role", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_item_permission", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_rls_role_member", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_onelake_role", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_onelake_role_member", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_onelake_rule", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_onelake_coverage", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_data_security", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_data_security_coverage", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_validation", "snapshot_date", "dim_date", "snapshot_date"),
        rel("fact_pipeline_run", "snapshot_date", "dim_date", "snapshot_date"),
        rel("bridge_group_membership", "snapshot_date", "dim_date", "snapshot_date"),

        # --- principal
        rel("fact_effective_access", "principal_id", "dim_principal", "principal_id"),
        rel("fact_access_summary", "principal_id", "dim_principal", "principal_id"),
        rel("fact_access_change", "principal_id", "dim_principal", "principal_id"),
        rel("fact_workspace_role", "principal_id", "dim_principal", "principal_id"),
        rel("fact_item_permission", "principal_id", "dim_principal", "principal_id"),
        rel("fact_rls_role_member", "principal_id", "dim_principal", "principal_id"),
        rel("fact_onelake_role_member", "principal_id", "dim_principal", "principal_id"),
        rel("fact_validation", "principal_id", "dim_principal", "principal_id"),
        # Rules with no member carry a null principal on purpose - an unassigned
        # role is a real finding, so the relationship must tolerate blanks.
        rel("fact_data_security", "principal_id", "dim_principal", "principal_id"),
        # The member side is the active path; group_id stays inactive so the
        # inverse "who is in this group" question can use USERELATIONSHIP.
        rel("bridge_group_membership", "principal_id", "dim_principal", "principal_id"),
        rel("bridge_group_membership", "group_id", "dim_principal", "principal_id", active=False),

        # --- item
        rel("fact_effective_access", "item_id", "dim_item", "item_id"),
        rel("fact_access_summary", "item_id", "dim_item", "item_id"),
        rel("fact_access_change", "item_id", "dim_item", "item_id"),
        rel("fact_item_permission", "item_id", "dim_item", "item_id"),
        rel("fact_rls_role_member", "item_id", "dim_item", "item_id"),
        rel("fact_onelake_role", "item_id", "dim_item", "item_id"),
        rel("fact_onelake_role_member", "item_id", "dim_item", "item_id"),
        rel("fact_onelake_rule", "item_id", "dim_item", "item_id"),
        rel("fact_onelake_coverage", "item_id", "dim_item", "item_id"),
        rel("fact_data_security", "item_id", "dim_item", "item_id"),
        rel("fact_data_security_coverage", "item_id", "dim_item", "item_id"),

        # --- workspace
        # Only fact_workspace_role joins dim_workspace directly; item-grained
        # facts reach it through dim_item, which keeps the paths unambiguous.
        rel("fact_workspace_role", "workspace_id", "dim_workspace", "workspace_id"),
        rel("dim_item", "workspace_id", "dim_workspace", "workspace_id"),

        # --- capacity
        rel("dim_workspace", "capacity_id", "dim_capacity", "capacity_id"),

        # --- onelake role chain
        rel("fact_onelake_role_member", "role_id", "fact_onelake_role", "role_id", active=False),
    ]

    # Measure names are global in a tabular model, not table-scoped. A collision
    # is only reported by the service after a full deploy round-trip, with a
    # message that names one measure and no location, so catch it locally.
    seen: dict[str, str] = {}
    for t in tables:
        for m in t.get("measures", []):
            prev = seen.get(m["name"])
            if prev:
                raise SystemExit(
                    f"Duplicate measure name {m['name']!r}: defined on both "
                    f"{prev!r} and {t['name']!r}. Measure names are model-wide."
                )
            seen[m["name"]] = t["name"]

    model = {
        "name": MODEL_NAME,
        "compatibilityLevel": 1604,
        "model": {
            "culture": "en-US",
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "sourceQueryCulture": "en-US",
            "discourageImplicitMeasures": True,
            "dataAccessOptions": {
                "legacyRedirects": True,
                "returnErrorValuesAsNull": True,
            },
            "expressions": [
                {
                    "name": "DatabaseQuery",
                    "kind": "m",
                    "lineageTag": tag(),
                    "expression": (
                        "let\n"
                        f'    database = Sql.Database("{SQL_ENDPOINT}", "{SQL_ENDPOINT_ID}")\n'
                        "in\n"
                        "    database"
                    ),
                    "annotations": [
                        {"name": "PBI_IncludeFutureArtifacts", "value": "False"}
                    ],
                }
            ],
            "tables": tables,
            "relationships": relationships,
            "annotations": [
                {"name": "PBI_QueryOrder", "value": json.dumps(["DatabaseQuery"])},
                {"name": "__PBI_TimeIntelligenceEnabled", "value": "0"},
                {"name": "PBIDesktopVersion", "value": "OneSafe"},
            ],
        },
    }
    return model


# ------------------------------------------------------------------- deployment

def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode()


def main() -> int:
    tok = token()

    model = build_model()
    (ROOT / "sm_onesafe.model.bim").write_text(json.dumps(model, indent=2), encoding="utf-8")

    pbism = {"version": "1.0", "settings": {}}

    definition = {
        "parts": [
            {"path": "model.bim", "payload": b64(json.dumps(model)), "payloadType": "InlineBase64"},
            {"path": "definition.pbism", "payload": b64(json.dumps(pbism)), "payloadType": "InlineBase64"},
        ]
    }

    existing = {
        m["displayName"]: m["id"]
        for m in request(
            "GET", f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}/semanticModels", tok=tok
        ).get("value", [])
    }

    if MODEL_NAME in existing:
        model_id = existing[MODEL_NAME]
        res = request(
            "POST",
            f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}/semanticModels/{model_id}/updateDefinition",
            {"definition": definition},
            tok,
        )
        await_operation(res.get("_location", ""), tok)
        print(f"updated semantic model {MODEL_NAME} -> {model_id}")
    else:
        res = request(
            "POST",
            f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}/semanticModels",
            {"displayName": MODEL_NAME,
             "description": "OneSafe security 360 - unified workspace, item and OneLake access.",
             "definition": definition},
            tok,
        )
        if not res.get("id"):
            res = await_operation(res.get("_location", ""), tok)
        model_id = res.get("id")
        print(f"created semantic model {MODEL_NAME} -> {model_id}")

    CFG["semanticModelId"] = model_id
    (ROOT / "config.json").write_text(json.dumps(CFG, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

