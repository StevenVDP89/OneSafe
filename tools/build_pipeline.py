"""Create/update the OneSafe daily orchestration Data Pipeline and its schedule.

The pipeline runs the extract -> transform -> gold -> refresh chain in order and
attaches a failure branch so an unattended break produces a readable incident
record instead of silence.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

FABRIC_API = "https://api.fabric.microsoft.com"
ROOT = Path(__file__).resolve().parent
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from onesafe_config import load as _load_config, load_notebook_ids as _load_notebook_ids
CONFIG = _load_config()
NB_IDS = _load_notebook_ids()

WORKSPACE_ID = CONFIG["workspaceId"]
PIPELINE_NAME = "pl_onesafe_daily"

# Ordered chain. Each entry runs only if its predecessor succeeded.
CHAIN = [
    ("01_extract_inventory", 30),
    ("02_extract_scanner", 120),
    ("03_extract_onelake", 90),
    ("04_extract_graph", 60),
    ("05_transform_silver", 60),
    ("06_build_gold", 90),
    ("07_build_changes", 30),
    ("08_validate", 60),
    ("09_refresh_model", 90),
]

SCHEDULE_TIME = "04:30"          # UTC, before the working day
SCHEDULE_TIMEZONE = "UTC"


def token(resource: str = FABRIC_API) -> str:
    return subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    ).stdout.strip()


def request(method: str, url: str, body: dict | None = None, tok: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            if resp.status == 202:
                return await_operation(resp.headers.get("Location"), tok)
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{exc.code} {method} {url}: {exc.read().decode()[:900]}"
        ) from None


def await_operation(location: str | None, tok: str, timeout_s: int = 600):
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
            raise RuntimeError(f"Operation failed: {json.dumps(state)[:600]}")
    raise TimeoutError(f"Operation {location} timed out")


def notebook_activity(name: str, timeout_min: int, depends: list[dict]) -> dict:
    return {
        "name": name,
        "type": "TridentNotebook",
        "dependsOn": depends,
        "policy": {
            "timeout": f"0.{timeout_min // 60:02d}:{timeout_min % 60:02d}:00",
            # Extraction steps hit throttled APIs; one automatic retry absorbs
            # transient 429/503 storms that the in-notebook backoff gave up on.
            "retry": 1,
            "retryIntervalInSeconds": 300,
            "secureOutput": False,
            "secureInput": False,
        },
        "typeProperties": {
            "notebookId": NB_IDS[name],
            "workspaceId": WORKSPACE_ID,
        },
    }


def build_pipeline_content() -> dict:
    activities = []
    depends: list[dict] = []
    for name, timeout_min in CHAIN:
        activities.append(notebook_activity(name, timeout_min, depends))
        depends = [{"activity": name, "dependencyConditions": ["Succeeded"]}]

    # Failure branch: fires if *any* step in the chain fails. Fabric evaluates
    # multiple dependencies as AND, so each failure edge needs its own activity;
    # a single "Failed" dependency on every step would require all of them to
    # fail. One handler per step keeps the semantics correct.
    for name, _ in CHAIN:
        activities.append(
            {
                "name": f"onfail_{name}",
                "type": "TridentNotebook",
                "dependsOn": [
                    {"activity": name, "dependencyConditions": ["Failed"]}
                ],
                "policy": {
                    "timeout": "0.00:20:00",
                    "retry": 0,
                    "retryIntervalInSeconds": 30,
                    "secureOutput": False,
                    "secureInput": False,
                },
                "typeProperties": {
                    "notebookId": NB_IDS["10_on_failure"],
                    "workspaceId": WORKSPACE_ID,
                    "parameters": {
                        "FAILED_ACTIVITY": {"value": name, "type": "string"},
                        "FAILED_MESSAGE": {
                            "value": f"@{{activity('{name}').Error.Message}}",
                            "type": "string",
                        },
                    },
                },
            }
        )

    return {
        "properties": {
            "description": (
                "OneSafe daily security scan: extract Fabric/Graph security "
                "metadata, build the gold star schema, refresh the SQL endpoint "
                "and the Direct Lake semantic model."
            ),
            "activities": activities,
            "annotations": ["OneSafe"],
        }
    }


def find_pipeline(tok: str) -> str | None:
    res = request(
        "GET", f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}/dataPipelines", tok=tok
    )
    for p in res.get("value", []):
        if p["displayName"] == PIPELINE_NAME:
            return p["id"]
    return None


def deploy_pipeline(tok: str) -> str:
    payload = base64.b64encode(
        json.dumps(build_pipeline_content()).encode()
    ).decode()
    definition = {
        "parts": [
            {
                "path": "pipeline-content.json",
                "payload": payload,
                "payloadType": "InlineBase64",
            }
        ]
    }

    existing = find_pipeline(tok)
    if existing:
        request(
            "POST",
            f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}/dataPipelines/{existing}/updateDefinition",
            {"definition": definition},
            tok,
        )
        print(f"updated pipeline {PIPELINE_NAME} -> {existing}")
        return existing

    res = request(
        "POST",
        f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}/dataPipelines",
        {"displayName": PIPELINE_NAME, "definition": definition},
        tok,
    )
    pid = res.get("id") or find_pipeline(tok)
    print(f"created pipeline {PIPELINE_NAME} -> {pid}")
    return pid


def ensure_schedule(pipeline_id: str, tok: str) -> None:
    base = (
        f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}"
        f"/items/{pipeline_id}/jobs/Pipeline/schedules"
    )
    body = {
        "enabled": True,
        "configuration": {
            "type": "Daily",
            "times": [SCHEDULE_TIME],
            "localTimeZoneId": SCHEDULE_TIMEZONE,
            "startDateTime": time.strftime("%Y-%m-%dT00:00:00"),
            "endDateTime": "2999-12-31T23:59:00",
        },
    }

    existing = request("GET", base, tok=tok).get("value", [])
    if existing:
        sid = existing[0]["id"]
        request("PATCH", f"{base}/{sid}", body, tok)
        print(f"updated schedule {sid} (daily {SCHEDULE_TIME} {SCHEDULE_TIMEZONE})")
    else:
        res = request("POST", base, body, tok)
        print(
            f"created schedule {res.get('id')} "
            f"(daily {SCHEDULE_TIME} {SCHEDULE_TIMEZONE})"
        )


def main() -> int:
    tok = token()
    missing = [n for n, _ in CHAIN if n not in NB_IDS]
    if "10_on_failure" not in NB_IDS:
        missing.append("10_on_failure")
    if missing:
        print(f"Missing notebook ids: {missing}. Run deploy_notebooks.py first.")
        return 1

    pipeline_id = deploy_pipeline(tok)
    CONFIG["pipelineId"] = pipeline_id
    (ROOT / "config.json").write_text(json.dumps(CONFIG, indent=2))

    if "--no-schedule" not in sys.argv:
        ensure_schedule(pipeline_id, tok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
