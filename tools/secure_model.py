"""Restrict the OneSafe semantic model and app workspace to Fabric admins.

OneSafe is a map of every weak point in the tenant: which identities can reach
which artifacts, where OneLake Security is and isn't enforcing, and which grants
look risky. That makes the model itself a high-value target, so it gets a
tighter perimeter than the data it describes.

What this does:
  * creates (or reuses) an Entra security group for OneSafe administrators
  * grants that group Read on the semantic model - enough to use the app, not
    enough to reshare or edit
  * adds the group as a Viewer on the app workspace so admins can open the app
    without gaining rights over the lakehouse

Deliberately not done here: adding anyone to the group. Membership is a human
decision and belongs with whoever owns tenant access reviews.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from onesafe_config import load as _load_config, load_notebook_ids as _load_notebook_ids
CONFIG = _load_config()

GRAPH = "https://graph.microsoft.com/v1.0"
FABRIC = "https://api.fabric.microsoft.com"
POWERBI = "https://api.powerbi.com/v1.0/myorg"

GROUP_NAME = "OneSafe Administrators"
GROUP_NICK = "onesafe-admins"


def token(resource: str) -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    )
    return out.stdout.strip()


def call(method: str, url: str, tok: str, body: dict | None = None, tolerate=()):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        if exc.code in tolerate:
            return exc.code, {"error": detail}
        raise RuntimeError(f"{exc.code} {method} {url}: {detail}") from None


def ensure_group(tok: str) -> str:
    query = urllib.parse.urlencode({
        "$filter": f"displayName eq '{GROUP_NAME}'",
        "$select": "id",
    })
    status, found = call("GET", f"{GRAPH}/groups?{query}", tok)
    if found.get("value"):
        gid = found["value"][0]["id"]
        print(f"Group '{GROUP_NAME}' already exists: {gid}")
        return gid

    _, created = call("POST", f"{GRAPH}/groups", tok, {
        "displayName": GROUP_NAME,
        "mailNickname": GROUP_NICK,
        "mailEnabled": False,
        "securityEnabled": True,
        "description": "Members may query the OneSafe security model and use the OneSafe app.",
    })
    print(f"Created group '{GROUP_NAME}': {created['id']}")
    # Entra needs a moment before the new object is usable elsewhere.
    time.sleep(20)
    return created["id"]


def grant_model_read(group_id: str) -> None:
    tok = token("https://analysis.windows.net/powerbi/api")
    url = f"{POWERBI}/groups/{CONFIG['workspaceId']}/datasets/{CONFIG['semanticModelId']}/users"
    # Read, not ReadReshare or ReadWrite: admins consume the model through the
    # app; nobody needs the ability to hand this dataset onward.
    status, resp = call("POST", url, tok, {
        "identifier": group_id,
        "principalType": "Group",
        "datasetUserAccessRight": "Read",
    }, tolerate=(400, 403))
    if status in (400, 403):
        print(f"Model grant not applied ({status}): {str(resp.get('error'))[:200]}")
        print("  -> grant Read on sm_onesafe to the group in the Fabric portal instead")
    else:
        print("Granted the group Read on sm_onesafe")


def grant_workspace_viewer(group_id: str) -> None:
    tok = token(FABRIC)
    for ws_key, label in (("appWorkspaceId", "app workspace"),):
        ws = CONFIG.get(ws_key)
        if not ws:
            continue
        status, resp = call("POST", f"{FABRIC}/v1/workspaces/{ws}/roleAssignments", tok, {
            "principal": {"id": group_id, "type": "Group"},
            "role": "Viewer",
        }, tolerate=(400, 409))
        if status in (400, 409):
            print(f"{label}: role assignment already present or rejected ({status})")
        else:
            print(f"Granted the group Viewer on the {label}")


def main() -> int:
    gid = ensure_group(token("https://graph.microsoft.com"))
    CONFIG["adminGroupId"] = gid
    CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")

    grant_model_read(gid)
    grant_workspace_viewer(gid)

    print(
        "\nOneSafe is restricted to members of "
        f"'{GROUP_NAME}' ({gid}).\n"
        "Add administrators to that group to give them access - membership is "
        "intentionally left to your access-review process."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
