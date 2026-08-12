"""Read a file from the OneSafe lakehouse Files area via the OneLake DFS API."""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

CFG = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8-sig"))
BASE = (
    f"https://onelake.dfs.fabric.microsoft.com/{CFG['workspaceId']}/"
    f"{CFG['lakehouseId']}"
)


def token() -> str:
    return subprocess.run(
        [
            "az", "account", "get-access-token",
            "--resource", "https://storage.azure.com",
            "--query", "accessToken", "-o", "tsv",
        ],
        capture_output=True, text=True, shell=True, check=True,
    ).stdout.strip()


def read(path: str, tok: str) -> str:
    req = urllib.request.Request(
        f"{BASE}/{path.lstrip('/')}",
        headers={"Authorization": "Bearer " + tok, "x-ms-version": "2021-06-08"},
    )
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode("utf-8", "replace")


def listdir(path: str, tok: str) -> list[str]:
    url = (
        f"https://onelake.dfs.fabric.microsoft.com/{CFG['workspaceId']}"
        f"?resource=filesystem&recursive=true&directory={CFG['lakehouseId']}/{path.lstrip('/')}"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + tok, "x-ms-version": "2021-06-08"}
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return [p["name"] for p in data.get("paths", [])]


if __name__ == "__main__":
    tok = token()
    target = sys.argv[1] if len(sys.argv) > 1 else "Files/diag"
    if target.endswith("/") or "." not in target.rsplit("/", 1)[-1]:
        for n in listdir(target, tok):
            print(n)
    else:
        print(read(target, tok))
