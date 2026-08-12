"""Trigger the OneSafe daily pipeline on demand and poll it to completion."""

from __future__ import annotations

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
WORKSPACE_ID = CONFIG["workspaceId"]
PIPELINE_ID = CONFIG["pipelineId"]


def token() -> str:
    return subprocess.run(
        ["az", "account", "get-access-token", "--resource", FABRIC_API,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    ).stdout.strip()


def call(method: str, url: str, tok: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        raw = resp.read().decode()
        return resp.status, resp.headers, (json.loads(raw) if raw else {})


def main() -> int:
    tok = token()
    base = f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}/items/{PIPELINE_ID}/jobs/instances"
    status, headers, _ = call("POST", base + "?jobType=Pipeline", tok, {})
    location = headers.get("Location")
    print(f"started pipeline run ({status}) -> {location}", flush=True)
    if not location:
        return 1

    deadline = time.time() + 60 * 180
    last = ""
    while time.time() < deadline:
        time.sleep(60)
        try:
            _, _, state = call("GET", location, tok)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                tok = token()
                continue
            raise
        st = state.get("status")
        if st != last:
            print(f"  {st}", flush=True)
            last = st
        if st in ("Completed", "Failed", "Cancelled", "Deduped"):
            print(json.dumps(state, indent=2)[:2500])
            return 0 if st == "Completed" else 1
    print("timed out")
    return 1


if __name__ == "__main__":
    sys.exit(main())
