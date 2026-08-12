"""Upload the OneSafe runtime config into the lakehouse Files area.

The config carries the scanner service principal credentials used by the
notebooks. It lives in OneLake inside the OneSafe workspace, which is restricted
to Fabric administrators - the same trust boundary as the security data itself.

Preferred hardening (blocked in this tenant by an Azure Policy that forces Key
Vault public network access off): store the secret in Key Vault and reach it
from Fabric over a managed private endpoint, then have 00_common call
notebookutils.credentials.getSecret instead of reading this file.

Usage:
    upload_config.py <client-secret>   full write, including the secret
    upload_config.py --sync            merge non-secret IDs into the existing
                                       file, preserving the stored secret
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
CONFIG = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8-sig"))

# Keys the notebooks need at runtime, beyond the credential itself. Keeping this
# list explicit stops unrelated local state leaking into the lakehouse.
#
# The demo* keys are only used by the optional 97/98 seeder notebooks. They are
# included so those notebooks work in any tenant without editing code; they are
# absent from config until `setup.py --with-demo` has run, and the seeders fail
# with a clear message rather than a KeyError when that is the case.
RUNTIME_KEYS = (
    "workspaceId", "lakehouseId", "sqlEndpointId", "semanticModelId",
    "demoWorkspaceId", "demoLakehouseId", "demoSemanticModelId", "demoPrincipals",
)


def token(resource: str) -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    )
    return out.stdout.strip()


def call(method: str, url: str, tok: str, body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{exc.code} {method} {url}: {exc.read().decode()[:500]}") from None


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not arg:
        print(__doc__)
        return 2

    sync_only = arg == "--sync"
    tok = token("https://storage.azure.com")
    base = f"{ONELAKE}/{CONFIG['workspaceId']}/{CONFIG['lakehouseId']}/Files/config/onesafe_config.json"

    if sync_only:
        # Read the current file so the secret never has to be re-supplied - and
        # never has to sit in shell history or a local scratch file - just to
        # add an item ID.
        _, raw = call("GET", base, tok)
        runtime = json.loads(raw.decode("utf-8-sig"))
        if not runtime.get("clientSecret"):
            print("existing config has no clientSecret; run with the secret instead")
            return 1
    else:
        runtime = {"clientSecret": arg}

    runtime["tenantId"] = CONFIG["tenantId"]
    runtime["clientId"] = CONFIG["scannerAppId"]
    for key in RUNTIME_KEYS:
        if CONFIG.get(key):
            runtime[key] = CONFIG[key]

    payload = json.dumps(runtime, indent=2).encode()

    # DFS create-then-append-then-flush.
    call("PUT", f"{base}?resource=file", tok)
    call("PATCH", f"{base}?action=append&position=0", tok, payload,
         {"Content-Type": "application/octet-stream"})
    call("PATCH", f"{base}?action=flush&position={len(payload)}", tok)

    shown = ", ".join(k for k in runtime if k != "clientSecret")
    print(f"Uploaded runtime config ({len(payload)} bytes): {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
