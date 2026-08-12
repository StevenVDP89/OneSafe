"""Create demonstration OneLake data access roles so the OneSafe app has real
data-plane security to display.

Why this exists
---------------
OneSafe reports OneLake Security accurately, but this tenant had exactly one
data access role (an item's implicit DefaultReader), so the OneLake pane
correctly reported "not used anywhere" and there was nothing to click through.
That is a true statement about the tenant, not a bug - but it makes the pane
impossible to evaluate.

This provisions a small, realistic set of roles on the dedicated demo lakehouse
`lh_onesafe_demo`:

  OneSafeDemoSalesReader        Ilaria        -> Tables/dbo/sales only
  OneSafeDemoSentimentReader    Ivana         -> Tables/dbo/sentiment only
  OneSafeDemoSharedReader       Ilaria+Ivana  -> Tables/dbo/reference only

That shape is chosen on purpose: two single-member roles with different scopes
plus one shared role gives the app a role with multiple members, a principal in
multiple roles, and per-path scoping to traverse.

Why a separate lakehouse
------------------------
OneLake Security is enabled per lakehouse, and the pre-existing demo lakehouses
in this tenant have it switched off - the API answers
`UniversalSecurityFeatureDisabledForWorkspace` for them. Rather than change the
security posture of somebody else's lakehouse to make a demo work, this creates
its own clearly-labelled, empty one.

It is NOT seeded against lh_onesafe. That lakehouse maps every weak point in the
tenant, and naming two ordinary users in roles on it - even read-scoped ones -
is the wrong default for a security tool.

The API replaces the whole role collection in one PUT, so existing roles are
read first and preserved. Re-running is idempotent - roles are matched by name
and rewritten rather than duplicated.

Usage:
  python tools/seed_onelake_roles.py            # create/update
  python tools/seed_onelake_roles.py --list     # show current roles
  python tools/seed_onelake_roles.py --remove   # remove only the seeded roles
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from onesafe_config import load as _load_config, load_notebook_ids as _load_notebook_ids
CFG = _load_config()

FABRIC = "https://api.fabric.microsoft.com"

# Dedicated sandbox workspace + lakehouse, resolved from config so this runs in
# any tenant. The demo principals are Viewers on the workspace, which is what
# gives them the item access these roles then scope.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from onesafe_config import demo as _demo  # noqa: E402

DEMO = _demo(CFG)
WORKSPACE_ID = DEMO["workspaceId"]
DEMO_LAKEHOUSE = DEMO["lakehouseId"]

# Entra object IDs of the demo users, in config order.
PRINCIPAL_A, PRINCIPAL_B = DEMO["objectIds"][0], DEMO["objectIds"][1]

PREFIX = "OneSafeDemo"


def token(resource: str = FABRIC) -> str:
    r = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    )
    return r.stdout.strip()


def api(method: str, url: str, tok: str, body: dict | None = None,
        extra_headers: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def roles_url(lakehouse_id: str) -> str:
    return f"{FABRIC}/v1/workspaces/{WORKSPACE_ID}/lakehouses/{lakehouse_id}/dataAccessRoles"


def read_roles(lakehouse_id: str, tok: str) -> tuple[list[dict], str | None]:
    """Return the current roles plus the collection ETag needed to write back."""
    url = roles_url(lakehouse_id)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
        etag = resp.headers.get("ETag")
    return payload.get("value", []), etag


def read_role(name: str, members: list[str], paths: list[str],
              *, columns: list[dict] | None = None,
              rows: list[dict] | None = None) -> dict:
    """A read-only role scoped to specific OneLake paths.

    Paths are relative to the item root, which is how the OneLake Security model
    expresses folder and table scoping.

    `columns` and `rows` attach OneLake column-level and row-level security to
    the rule. They are the data-plane equivalents of a semantic model's object
    and row level security, and they are what makes "has access to the table"
    an incomplete answer.
    """
    constraints: dict = {}
    if columns:
        constraints["columns"] = columns
    if rows:
        constraints["rows"] = rows

    rule: dict = {
        "effect": "Permit",
        "permission": [
            {"attributeName": "Path", "attributeValueIncludedIn": paths},
            {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]},
        ],
    }
    if constraints:
        rule["constraints"] = constraints

    return {
        "name": name,
        "kind": "Policy",
        "decisionRules": [rule],
        "members": {
            "microsoftEntraMembers": [{"objectId": oid, "tenantId": CFG["tenantId"]}
                                      for oid in members]
        },
    }


def cls(table_path: str, *column_names: str) -> dict:
    """Deny-by-omission column security: only the named columns are readable."""
    return {
        "tablePath": table_path,
        "columnNames": list(column_names),
        "columnEffect": "Permit",
        "columnAction": ["Read"],
    }


def rls(table_path: str, predicate: str) -> dict:
    """A row filter, expressed as a SQL SELECT over the table.

    The service validates this predicate against the real table schema, which
    means three things have to line up or the PUT is rejected with
    `InvalidRLSPredicate`:

      * the table must actually exist (see notebooks/97_seed_demo_lakehouse.py);
      * the FROM clause must be schema-qualified - `dbo.customer`, not
        `customer`, even though the path already names the schema;
      * string literals must use single quotes. Double quotes are read as
        identifiers and fail.
    """
    return {"tablePath": table_path, "value": predicate}


DESIRED = {
    DEMO_LAKEHOUSE: [
        # Path scoping only - the simplest shape, and the baseline the app
        # should distinguish from the constrained roles below.
        read_role(f"{PREFIX}SalesReader", [PRINCIPAL_A], ["/Tables/dbo/sales"]),
        read_role(f"{PREFIX}SentimentReader", [PRINCIPAL_B], ["/Tables/dbo/sentiment"]),
        read_role(f"{PREFIX}SharedReader", [PRINCIPAL_A, PRINCIPAL_B], ["/Tables/dbo/reference"]),

        # Column-level security: principal A can read the customer table but not
        # the two PII columns on it.
        read_role(
            f"{PREFIX}CustomerNoPII", [PRINCIPAL_A], ["/Tables/dbo/customer"],
            columns=[cls("/Tables/dbo/customer", "CustomerId", "CustomerName", "Region", "Segment")],
        ),

        # Row-level security: principal B sees only AMER rows of the same table.
        read_role(
            f"{PREFIX}CustomerAMER", [PRINCIPAL_B], ["/Tables/dbo/customer"],
            rows=[rls("/Tables/dbo/customer", "SELECT * FROM dbo.customer WHERE Region = 'AMER'")],
        ),

        # Both at once - the case an admin most needs to see spelled out.
        read_role(
            f"{PREFIX}FinanceRestricted", [PRINCIPAL_A, PRINCIPAL_B], ["/Tables/dbo/transactions"],
            columns=[cls("/Tables/dbo/transactions", "TransactionId", "Region", "Amount")],
            rows=[rls("/Tables/dbo/transactions",
                      "SELECT * FROM dbo.transactions WHERE Region <> 'APAC'")],
        ),
    ],
}


def merge(existing: list[dict], desired: list[dict]) -> list[dict]:
    """Keep every role we did not author; replace ours by name."""
    by_name = {r["name"]: r for r in desired}
    out = [r for r in existing if r.get("name") not in by_name]
    out.extend(desired)
    return out


def strip_seeded(existing: list[dict]) -> list[dict]:
    return [r for r in existing if not str(r.get("name", "")).startswith(PREFIX)]


def write_roles(lakehouse_id: str, roles: list[dict], etag: str | None, tok: str) -> None:
    # The API replaces the whole collection; the ETag guards against clobbering a
    # concurrent change made in the portal.
    headers = {"If-Match": etag} if etag else {}
    status, body = api("PUT", roles_url(lakehouse_id), tok,
                       {"value": roles}, extra_headers=headers)
    if status not in (200, 201, 202):
        raise SystemExit(f"PUT {lakehouse_id} failed ({status}): "
                         f"{json.dumps(body)[:600] if isinstance(body, dict) else body[:600]}")


def describe(roles: list[dict]) -> None:
    for r in roles:
        members = r.get("members", {}) or {}
        entra = members.get("microsoftEntraMembers", []) or []
        items = members.get("fabricItemMembers", []) or []
        paths = [
            v
            for rule in r.get("decisionRules", [])
            for perm in rule.get("permission", [])
            if perm.get("attributeName") == "Path"
            for v in perm.get("attributeValueIncludedIn", [])
        ]
        print(f"    - {r.get('name')}  ({len(entra)} entra member(s), "
              f"{len(items)} item member(s))  paths={paths or ['*']}")
        for rule in r.get("decisionRules", []):
            cons = rule.get("constraints") or {}
            for c in cons.get("columns") or []:
                print(f"        CLS  {c.get('tablePath')} -> readable: {c.get('columnNames')}")
            for rw in cons.get("rows") or []:
                print(f"        RLS  {rw.get('tablePath')} -> {rw.get('value')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show current roles and exit")
    ap.add_argument("--remove", action="store_true", help="remove only the seeded roles")
    args = ap.parse_args()

    tok = token()

    for lakehouse_id in DESIRED:
        existing, etag = read_roles(lakehouse_id, tok)
        print(f"\n{lakehouse_id}: {len(existing)} existing role(s)")
        describe(existing)

        if args.list:
            continue

        target = strip_seeded(existing) if args.remove else merge(existing, DESIRED[lakehouse_id])
        write_roles(lakehouse_id, target, etag, tok)

        after, _ = read_roles(lakehouse_id, tok)
        print(f"  -> now {len(after)} role(s)")
        describe(after)

    if not args.list:
        print("\nRun the pipeline (or just 03_extract_onelake + downstream) to surface these "
              "in the model:\n  python tools/run_pipeline.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
