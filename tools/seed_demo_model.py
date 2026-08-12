"""Create a demonstration semantic model carrying real RLS and OLS/CLS roles.

Why this exists
---------------
OneSafe now reports row-level and column-level security, but this tenant had no
semantic model defining either, so those panes would correctly - and uselessly -
report "none found". The same problem the OneLake demo roles solved, one layer
up.

This provisions `sm_onesafe_demo` in the dedicated OneSafe Demo workspace with:

  DemoRegionEMEA    user A   RLS  Sales filtered to Region = "EMEA"
  DemoRegionAMER    user B   RLS  Sales filtered to Region = "AMER"
                                  Customer filtered to the same region
  DemoNoPII         user B   CLS  Sales[CreditCardNumber] and
                                  Customer[TaxId] hidden (metadataPermission none)
  DemoAuditReadOnly user A   RLS+CLS - a dynamic USERPRINCIPALNAME() filter plus
                                  a hidden column, so the app has one role that
                                  exercises both mechanisms at once

Users A and B are the first two entries of `demoPrincipals` in tools/config.json,
so this runs in any tenant. They must be real users: the dynamic role filters on
USERPRINCIPALNAME(), and a filter naming nobody returns nothing and looks broken
rather than restrictive.

That shape is deliberate: a static filter, a dynamic filter, a pure column
restriction, and a combined role. It gives the Data Security pane a role with
two members, a principal in two roles, and both rule kinds to traverse.

Why calculated tables
---------------------
The tables are DAX `DATATABLE` literals, so the model has no data source, no
gateway and no lakehouse dependency. It can be created and refreshed anywhere,
and it cannot leak real tenant data into a demo artifact.

Why not on sm_onesafe
---------------------
sm_onesafe maps every weak point in the tenant. Adding RLS roles naming ordinary
users to it would both restrict the admin app and put those users in the blast
radius of a security model. Demo security belongs on a demo artifact.

Usage:
  python tools/seed_demo_model.py           # create or update
  python tools/seed_demo_model.py --show    # print the roles currently deployed
  python tools/seed_demo_model.py --remove  # delete the demo model
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CFG = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8-sig"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from onesafe_config import demo as _demo  # noqa: E402

FABRIC = "https://api.fabric.microsoft.com"
MODEL_NAME = "sm_onesafe_demo"

# Sandbox workspace and demo users, resolved from config so this runs in any
# tenant. Each entry is (entra object id, upn) — RLS members need both.
DEMO = _demo(CFG)
DEMO_WORKSPACE = DEMO["workspaceId"]
PRINCIPAL_A = (DEMO["principals"][0]["objectId"], DEMO["principals"][0]["upn"])
PRINCIPAL_B = (DEMO["principals"][1]["objectId"], DEMO["principals"][1]["upn"])


# ----------------------------------------------------------------- transport

def token(resource: str = FABRIC) -> str:
    return subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    ).stdout.strip()


def call(method: str, url: str, tok: str, body: dict | None = None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, dict(resp.headers), (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, dict(exc.headers), json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, dict(exc.headers), raw


def await_lro(status: int, headers: dict, body, tok: str, want_result: bool = False):
    """Fabric item writes are long-running; poll Location until terminal."""
    if status != 202:
        return status, body
    loc = headers.get("Location")
    for _ in range(60):
        time.sleep(3)
        st, _h, b = call("GET", loc, tok)
        if isinstance(b, dict) and b.get("status") in ("Succeeded", "Failed"):
            if b.get("status") == "Failed":
                return 500, b
            if want_result:
                return call("GET", loc.rstrip("/") + "/result", tok)[::2]
            return 200, b
    return 504, {"error": "operation did not complete"}


# --------------------------------------------------------------- model build

def dt_col(name: str, dtype: str) -> dict:
    """A calculated-table column: the engine derives it from the DAX partition."""
    return {
        "name": name,
        "dataType": dtype,
        "sourceColumn": f"[{name}]",
        "type": "calculatedTableColumn",
    }


def calc_table(name: str, columns: list[tuple[str, str]], expression: str) -> dict:
    return {
        "name": name,
        "columns": [dt_col(c, t) for c, t in columns],
        "partitions": [
            {
                "name": name,
                "mode": "import",
                "source": {"type": "calculated", "expression": expression},
            }
        ],
    }


# The SalesRep column carries real UPNs on purpose: DemoAuditReadOnly filters
# it with USERPRINCIPALNAME(), so unless these match actual signed-in users the
# dynamic RLS role would silently return nothing and look broken rather than
# restrictive. Built from config, with the caller as the third rep.
_UPN_A, _UPN_B = PRINCIPAL_A[1], PRINCIPAL_B[1]
_UPN_C = (DEMO["upns"][2] if len(DEMO["upns"]) > 2 else _UPN_A)

SALES_DATA = f"""DATATABLE(
    "Region", STRING, "Country", STRING, "CustomerName", STRING,
    "CreditCardNumber", STRING, "SalesRep", STRING, "Amount", DOUBLE,
    {{
        {{"EMEA", "Belgium", "Contoso NV", "4111-1111-1111-1111", "{_UPN_A}", 128400.00}},
        {{"EMEA", "Germany", "Fabrikam GmbH", "4111-2222-2222-2222", "{_UPN_A}", 94250.00}},
        {{"EMEA", "France", "Litware SARL", "4111-3333-3333-3333", "{_UPN_A}", 61800.00}},
        {{"AMER", "United States", "Adventure Works", "5222-1111-1111-1111", "{_UPN_B}", 211900.00}},
        {{"AMER", "Canada", "Northwind Traders", "5222-2222-2222-2222", "{_UPN_B}", 76400.00}},
        {{"APAC", "Japan", "Tailspin KK", "6333-1111-1111-1111", "{_UPN_C}", 154300.00}},
        {{"APAC", "Australia", "Wide World Pty", "6333-2222-2222-2222", "{_UPN_C}", 88750.00}}
    }}
)"""

CUSTOMER_DATA = """DATATABLE(
    "CustomerName", STRING, "Region", STRING, "TaxId", STRING,
    "ContactEmail", STRING, "CreditLimit", DOUBLE,
    {
        {"Contoso NV", "EMEA", "BE0123456789", "ap@contoso.example", 250000.00},
        {"Fabrikam GmbH", "EMEA", "DE811234567", "finance@fabrikam.example", 180000.00},
        {"Litware SARL", "EMEA", "FR12345678901", "compta@litware.example", 120000.00},
        {"Adventure Works", "AMER", "US-94-1234567", "ap@adventure.example", 400000.00},
        {"Northwind Traders", "AMER", "US-94-7654321", "ap@northwind.example", 150000.00},
        {"Tailspin KK", "APAC", "JP-1234567890", "keiri@tailspin.example", 200000.00},
        {"Wide World Pty", "APAC", "AU-12345678901", "ap@wideworld.example", 160000.00}
    }
)"""


def member(principal: tuple[str, str]) -> dict:
    # TMSL ModelRoleMember. `memberType` is rejected silently at this
    # compatibility level - including it caused the whole members array to be
    # dropped on deploy - so only the three documented properties are sent.
    object_id, upn = principal
    return {
        "memberName": upn,
        "memberId": object_id,
        "identityProvider": "AzureAD",
    }


def build_model() -> dict:
    return {
        "name": MODEL_NAME,
        "compatibilityLevel": 1567,
        "model": {
            "culture": "en-US",
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "discourageImplicitMeasures": True,
            "sourceQueryCulture": "en-US",
            "tables": [
                calc_table(
                    "Sales",
                    [("Region", "string"), ("Country", "string"), ("CustomerName", "string"),
                     ("CreditCardNumber", "string"), ("SalesRep", "string"), ("Amount", "double")],
                    SALES_DATA,
                ),
                calc_table(
                    "Customer",
                    [("CustomerName", "string"), ("Region", "string"), ("TaxId", "string"),
                     ("ContactEmail", "string"), ("CreditLimit", "double")],
                    CUSTOMER_DATA,
                ),
            ],
            "relationships": [
                {
                    "name": "Sales_Customer",
                    "fromTable": "Sales",
                    "fromColumn": "CustomerName",
                    "toTable": "Customer",
                    "toColumn": "CustomerName",
                }
            ],
            # The point of the whole exercise: four roles spanning static RLS,
            # dynamic RLS, pure CLS, and a combined role.
            "roles": [
                {
                    "name": "DemoRegionEMEA",
                    "modelPermission": "read",
                    "description": "OneSafe demo - EMEA analysts see only EMEA rows.",
                    "tablePermissions": [
                        {"name": "Sales", "filterExpression": "Sales[Region] = \"EMEA\""},
                        {"name": "Customer", "filterExpression": "Customer[Region] = \"EMEA\""},
                    ],
                    "members": [member(PRINCIPAL_A)],
                },
                {
                    "name": "DemoRegionAMER",
                    "modelPermission": "read",
                    "description": "OneSafe demo - AMER analysts see only AMER rows.",
                    "tablePermissions": [
                        {"name": "Sales", "filterExpression": "Sales[Region] = \"AMER\""},
                        {"name": "Customer", "filterExpression": "Customer[Region] = \"AMER\""},
                    ],
                    "members": [member(PRINCIPAL_B)],
                },
                {
                    "name": "DemoNoPII",
                    "modelPermission": "read",
                    "description": "OneSafe demo - column-level security hiding PII columns.",
                    "tablePermissions": [
                        {
                            "name": "Sales",
                            "columnPermissions": [
                                {"name": "CreditCardNumber", "metadataPermission": "none"}
                            ],
                        },
                        {
                            "name": "Customer",
                            "columnPermissions": [
                                {"name": "TaxId", "metadataPermission": "none"},
                                {"name": "ContactEmail", "metadataPermission": "none"},
                            ],
                        },
                    ],
                    "members": [member(PRINCIPAL_B)],
                },
                {
                    "name": "DemoAuditReadOnly",
                    "modelPermission": "read",
                    "description": "OneSafe demo - dynamic RLS by signed-in user, plus hidden PII.",
                    "tablePermissions": [
                        {
                            "name": "Sales",
                            "filterExpression": "Sales[SalesRep] = USERPRINCIPALNAME()",
                            "columnPermissions": [
                                {"name": "CreditCardNumber", "metadataPermission": "none"}
                            ],
                        }
                    ],
                    "members": [member(PRINCIPAL_A)],
                },
            ],
        },
    }


PBISM = {
    "version": "1.0",
    "settings": {"qnaEnabled": False},
}


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode()


def definition() -> dict:
    return {
        "parts": [
            {"path": "definition.pbism", "payload": b64(json.dumps(PBISM)), "payloadType": "InlineBase64"},
            {"path": "model.bim", "payload": b64(json.dumps(build_model(), indent=2)), "payloadType": "InlineBase64"},
        ]
    }


# ------------------------------------------------------------------- actions

def find_model(tok: str) -> str | None:
    st, _h, body = call("GET", f"{FABRIC}/v1/workspaces/{DEMO_WORKSPACE}/semanticModels", tok)
    if st != 200 or not isinstance(body, dict):
        raise SystemExit(f"list semanticModels failed ({st}): {str(body)[:400]}")
    for it in body.get("value", []):
        if it.get("displayName") == MODEL_NAME:
            return it["id"]
    return None


def show(tok: str, model_id: str) -> None:
    st, hdr, body = call(
        "POST", f"{FABRIC}/v1/workspaces/{DEMO_WORKSPACE}/semanticModels/{model_id}/getDefinition?format=TMSL", tok)
    if st == 202:
        loc = hdr["Location"]
        for _ in range(40):
            time.sleep(3)
            s2, _h2, b2 = call("GET", loc, tok)
            if isinstance(b2, dict) and b2.get("status") in ("Succeeded", "Failed"):
                st, _h3, body = call("GET", loc.rstrip("/") + "/result", tok)
                break
    if st != 200 or not isinstance(body, dict):
        print(f"  could not read definition ({st}): {str(body)[:300]}")
        return
    for part in body.get("definition", {}).get("parts", []):
        if not part["path"].endswith(".bim"):
            continue
        model = json.loads(base64.b64decode(part["payload"]).decode("utf-8"))["model"]
        for role in model.get("roles", []):
            members = ", ".join(m.get("memberName", "?") for m in role.get("members", []))
            print(f"    - {role['name']}  [{members or 'no members'}]")
            for tp in role.get("tablePermissions", []):
                if tp.get("filterExpression"):
                    print(f"        RLS  {tp['name']}: {tp['filterExpression']}")
                for cp in tp.get("columnPermissions", []):
                    print(f"        CLS  {tp['name']}.{cp['name']}: {cp.get('metadataPermission')}")


def refresh(tok: str, model_id: str) -> None:
    """Calculated tables hold no data until the model is processed once."""
    pbi = token("https://analysis.windows.net/powerbi/api")
    st, _h, body = call(
        "POST",
        f"https://api.powerbi.com/v1.0/myorg/groups/{DEMO_WORKSPACE}/datasets/{model_id}/refreshes",
        pbi,
        {"type": "full"},
    )
    print(f"  refresh requested: {st}" + ("" if st in (200, 202) else f" {str(body)[:300]}"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true", help="print deployed roles and exit")
    ap.add_argument("--remove", action="store_true", help="delete the demo model")
    ap.add_argument("--no-refresh", action="store_true", help="skip the post-deploy refresh")
    args = ap.parse_args()

    tok = token()
    model_id = find_model(tok)

    if args.show:
        if not model_id:
            print(f"{MODEL_NAME} does not exist in the demo workspace.")
            return 0
        print(f"{MODEL_NAME} ({model_id}) roles:")
        show(tok, model_id)
        return 0

    if args.remove:
        if not model_id:
            print("nothing to remove")
            return 0
        st, _h, body = call("DELETE", f"{FABRIC}/v1/workspaces/{DEMO_WORKSPACE}/semanticModels/{model_id}", tok)
        print(f"deleted ({st}) {str(body)[:200]}")
        return 0

    if model_id:
        print(f"updating existing {MODEL_NAME} ({model_id})")
        st, hdr, body = call(
            "POST",
            f"{FABRIC}/v1/workspaces/{DEMO_WORKSPACE}/semanticModels/{model_id}/updateDefinition",
            tok,
            {"definition": definition()},
        )
        st, body = await_lro(st, hdr, body, tok)
    else:
        print(f"creating {MODEL_NAME}")
        st, hdr, body = call(
            "POST",
            f"{FABRIC}/v1/workspaces/{DEMO_WORKSPACE}/semanticModels",
            tok,
            {"displayName": MODEL_NAME,
             "description": "OneSafe demo model carrying RLS and column-level security roles.",
             "definition": definition()},
        )
        st, body = await_lro(st, hdr, body, tok)
        model_id = find_model(tok)

    if st not in (200, 201):
        raise SystemExit(f"deploy failed ({st}): {json.dumps(body)[:900] if isinstance(body, dict) else str(body)[:900]}")

    print(f"  deployed: {model_id}")
    if not args.no_refresh:
        refresh(tok, model_id)

    print("\nroles now deployed:")
    show(tok, model_id)
    print("\nRun the pipeline to surface these in OneSafe:\n  python tools/run_pipeline.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
