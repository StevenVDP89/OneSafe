"""Run a DAX query against the OneSafe semantic model via the Power BI REST API."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
MODEL_ID = CFG["semanticModelId"]


def token() -> str:
    return subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", "https://analysis.windows.net/powerbi/api",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    ).stdout.strip()


def query(dax: str, tok: str) -> dict:
    url = f"https://api.powerbi.com/v1.0/myorg/datasets/{MODEL_ID}/executeQueries"
    body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{exc.code}: {exc.read().decode()[:1500]}") from None


DEFAULT = [
    'EVALUATE ROW("paths", [Access Paths], "principals", [Principals with Access], '
    '"items", [Items Accessible], "risk", [Risk Paths], "orphaned", [Orphaned Access Paths], '
    '"overpriv", [Over-Privileged Score])',
    'EVALUATE TOPN(5, SUMMARIZECOLUMNS(\'dim_principal\'[display_name], '
    '\'dim_principal\'[principal_type], "paths", [Access Paths]), [Access Paths], DESC)',
    'EVALUATE SUMMARIZECOLUMNS(\'fact_effective_access\'[grant_source], "n", [Access Paths])',
]


def main() -> int:
    tok = token()
    args = sys.argv[1:]
    if args and args[0] == "-f":
        # Read queries from a file, separated by lines containing only "---".
        text = Path(args[1]).read_text(encoding="utf-8")
        queries = [q.strip() for q in text.split("\n---\n") if q.strip()]
    else:
        queries = args or DEFAULT
    for dax in queries:
        print(f"--- {dax[:110]}")
        res = query(dax, tok)
        for t in res.get("results", []):
            for tbl in t.get("tables", []):
                for row in tbl.get("rows", [])[:20]:
                    print("   ", json.dumps(row, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
