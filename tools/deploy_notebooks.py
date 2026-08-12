"""Deploy OneSafe notebooks into the Fabric workspace.

Converts the ``# CELL ********************`` delimited .py sources into
Jupyter notebooks and creates/updates them via the Fabric item definition API.
Uses the Azure CLI's logged-in identity for authentication.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

FABRIC_API = "https://api.fabric.microsoft.com"
NOTEBOOK_DIR = Path(__file__).resolve().parent.parent / "notebooks"

CONFIG = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8-sig"))
WORKSPACE_ID = CONFIG["workspaceId"]
LAKEHOUSE_ID = CONFIG["lakehouseId"]
LAKEHOUSE_NAME = CONFIG["lakehouseName"]

CELL_DELIM = re.compile(r"^# CELL \*+\s*$", re.MULTILINE)


def token() -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", FABRIC_API,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    )
    return out.stdout.strip()


def request(method: str, url: str, body: dict | None = None, tok: str | None = None):
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            # Item create/update are long-running operations: 202 + Location.
            if resp.status == 202:
                return await_operation(resp.headers.get("Location"), tok)
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{exc.code} {method} {url}: {exc.read().decode()[:800]}") from None


def await_operation(location: str | None, tok: str, timeout_s: int = 600):
    """Poll a Fabric long-running operation until it terminates."""
    if not location:
        return {}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        state = request("GET", location, tok=tok)
        status = state.get("status")
        if status == "Succeeded":
            # The result endpoint carries the created item (when there is one).
            try:
                return request("GET", location.rstrip("/") + "/result", tok=tok) or {}
            except RuntimeError:
                return {}
        if status == "Failed":
            raise RuntimeError(f"Operation failed: {json.dumps(state)[:600]}")
    raise TimeoutError(f"Operation {location} timed out")


def to_ipynb(source: str, name: str = "notebook") -> dict:
    """Split a delimited .py source file into notebook cells.

    ``%run 00_common`` is resolved at deploy time by inlining the shared module,
    so notebooks have no cross-notebook runtime dependency.
    """
    common_path = Path(__file__).resolve().parent.parent / "notebooks" / "00_common.py"
    chunks = [c.strip("\n") for c in CELL_DELIM.split(source)]
    cells = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        lines = chunk.split("\n")
        stripped = [
            l.replace("# MAGIC ", "").replace("# MAGIC", "")
            if l.strip().startswith("# MAGIC")
            else l
            for l in lines
        ]
        if any(l.strip() == "%run 00_common" for l in stripped):
            common_src = common_path.read_text(encoding="utf-8")
            for sub in CELL_DELIM.split(common_src):
                sub = sub.strip("\n")
                if sub.strip():
                    cells.append(_cell(sub.split("\n")))
            cells.append(_cell([f'NOTEBOOK_NAME = "{name}"']))
            continue
        cells.append(_cell(stripped))

    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "cells": cells,
        "metadata": {
            "language_info": {"name": "python"},
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
            "dependencies": {
                "lakehouse": {
                    "default_lakehouse": LAKEHOUSE_ID,
                    "default_lakehouse_name": LAKEHOUSE_NAME,
                    "default_lakehouse_workspace_id": WORKSPACE_ID,
                }
            },
        },
    }


def _cell(lines: list[str]) -> dict:
    return {
        "cell_type": "code",
        "source": [l + "\n" for l in lines],
        "execution_count": None,
        "outputs": [],
        "metadata": {},
    }


def b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def existing_notebooks(tok: str) -> dict[str, str]:
    res = request("GET", f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}/notebooks", tok=tok)
    return {n["displayName"]: n["id"] for n in res.get("value", [])}


def deploy(path: Path, tok: str, existing: dict[str, str]) -> str:
    name = path.stem
    payload = b64(to_ipynb(path.read_text(encoding="utf-8"), name))
    definition = {
        "format": "ipynb",
        "parts": [
            {"path": "notebook-content.ipynb", "payload": payload, "payloadType": "InlineBase64"}
        ],
    }

    if name in existing:
        request(
            "POST",
            f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}/notebooks/{existing[name]}/updateDefinition",
            {"definition": definition},
            tok,
        )
        print(f"  updated  {name}", flush=True)
        return existing[name]

    res = request(
        "POST",
        f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}/notebooks",
        {"displayName": name, "definition": definition},
        tok,
    )
    nb_id = res.get("id") or existing_notebooks(tok).get(name)
    print(f"  created  {name} -> {nb_id}", flush=True)
    return nb_id


def main() -> int:
    tok = token()
    existing = existing_notebooks(tok)
    print(f"Deploying to workspace {WORKSPACE_ID}")

    ids = {}
    for path in sorted(NOTEBOOK_DIR.glob("*.py")):
        ids[path.stem] = deploy(path, tok, existing)

    out = Path(__file__).resolve().parent / "notebook_ids.json"
    out.write_text(json.dumps(ids, indent=2))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
