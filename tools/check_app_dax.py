"""Execute every DAX query the OneSafe front-end can issue and report failures.

Pairs with tools/capture_app_dax.js, which runs the panes headlessly and records
their queries. This script runs each captured query against the live semantic
model, so a query that references a renamed column or a measure that never
existed fails here - loudly - instead of rendering as an empty panel that an
admin has to notice and distrust.

Usage:
    node tools/capture_app_dax.js && python tools/check_app_dax.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from onesafe_config import load as _load_config, load_notebook_ids as _load_notebook_ids
CONFIG = _load_config()
QUERIES = HERE / "_app_queries.json"

POWERBI = "https://api.powerbi.com/v1.0/myorg"


def token() -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", "https://analysis.windows.net/powerbi/api",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    )
    return out.stdout.strip()


def execute(dax: str, tok: str) -> tuple[bool, str, int]:
    """Return (ok, message, row_count)."""
    body = json.dumps({
        "queries": [{"query": dax}],
        "serializerSettings": {"includeNulls": True},
    }).encode()
    url = f"{POWERBI}/datasets/{CONFIG['semanticModelId']}/executeQueries"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode())
        rows = payload["results"][0]["tables"][0]["rows"]
        return True, "", len(rows)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        # Dig out the human-readable reason; the envelope is deeply nested.
        detail = raw
        try:
            err = json.loads(raw).get("error", {})
            for d in err.get("pbi.error", {}).get("details", []):
                if d.get("code") == "DetailsMessage":
                    detail = d["detail"]["value"]
                    break
        except Exception:  # noqa: BLE001
            pass
        return False, f"HTTP {exc.code}: {detail}", 0
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", 0


def main() -> int:
    if not QUERIES.exists():
        print("No captured queries. Run: node tools/capture_app_dax.js")
        return 2

    captured = json.loads(QUERIES.read_text(encoding="utf-8"))

    # The same query often appears under several panes/filter states; running it
    # once is enough and keeps the check fast enough to actually be re-run.
    unique: dict[str, str] = {}
    for entry in captured:
        unique.setdefault(entry["dax"], entry["pane"])

    print(f"{len(captured)} captured queries, {len(unique)} unique\n")

    tok = token()
    failures = []
    empty = []
    per_pane = Counter()

    for i, (dax, pane) in enumerate(unique.items(), 1):
        ok, msg, rows = execute(dax, tok)
        if ok:
            per_pane[pane.split("/")[0]] += 1
            if rows == 0:
                empty.append((pane, dax))
            print(f"  [{i:>3}/{len(unique)}] ok   {pane:<28} {rows:>5} rows")
        else:
            failures.append((pane, dax, msg))
            print(f"  [{i:>3}/{len(unique)}] FAIL {pane:<28} {msg[:160]}")

    print(f"\n{'=' * 70}")
    print(f"passed: {len(unique) - len(failures)}/{len(unique)}")
    if empty:
        # Not an error: several panes legitimately return nothing in this tenant
        # (no access changes until a second snapshot exists, for example).
        print(f"empty results (may be legitimate): {len(empty)}")
        for pane, _ in empty[:15]:
            print(f"  - {pane}")

    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for pane, dax, msg in failures:
            print(f"\n--- {pane}\n{msg}\n{dax[:700]}")
        return 1

    print("\nAll front-end queries execute cleanly against the live model.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
