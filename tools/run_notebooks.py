"""Run OneSafe notebooks on demand and stream their status.

Uses the Fabric job scheduler (``jobs/instances?jobType=RunNotebook``) so the
notebooks execute exactly as they will under the daily pipeline.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

FABRIC_API = "https://api.fabric.microsoft.com"
HERE = Path(__file__).resolve().parent
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from onesafe_config import load as _load_config, load_notebook_ids as _load_notebook_ids
CONFIG = _load_config()
IDS = _load_notebook_ids()
WS = CONFIG["workspaceId"]


def token() -> str:
    return subprocess.run(
        ["az", "account", "get-access-token", "--resource", FABRIC_API,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    ).stdout.strip()


def call(method: str, url: str, tok: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return resp.status, resp.headers, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, {"error": exc.read().decode()[:800]}


def run(name: str, tok: str, timeout_min: int = 90) -> str:
    nb_id = IDS[name]
    status, headers, body = call(
        "POST",
        f"{FABRIC_API}/v1/workspaces/{WS}/items/{nb_id}/jobs/instances?jobType=RunNotebook",
        tok,
        {"executionData": {}},
    )
    if status not in (200, 201, 202):
        raise RuntimeError(f"Failed to start {name}: {status} {body}")

    location = headers.get("Location")
    print(f"  started  {name}", flush=True)

    deadline = time.time() + timeout_min * 60
    last = None
    while time.time() < deadline:
        time.sleep(20)
        _, _, state = call("GET", location, tok)
        js = state.get("status")
        if js != last:
            print(f"    {name}: {js}", flush=True)
            last = js
        if js in ("Completed", "Failed", "Cancelled", "Deduped"):
            if js != "Completed":
                fail = state.get("failureReason") or {}
                raise RuntimeError(f"{name} -> {js}: {json.dumps(fail)[:900]}")
            return js
    raise TimeoutError(f"{name} did not finish within {timeout_min}m")


def main() -> int:
    order = sys.argv[1:] or [
        "01_extract_inventory",
        "02_extract_scanner",
        "03_extract_onelake",
        "04_extract_graph",
        "05_transform_silver",
        "06_build_gold",
        "07_build_changes",
        "08_validate",
    ]
    tok = token()
    for name in order:
        print(f"\n=== {name} ===", flush=True)
        run(name, tok)
    print("\nAll steps completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
