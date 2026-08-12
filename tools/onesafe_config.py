"""Shared config access for the OneSafe host-side tools.

Every tool needs the same three things: the config file, a clear failure when a
key it depends on has not been provisioned yet, and the demo-sandbox settings.
Doing that in one place means a missing key produces one good error message
instead of a `KeyError` or — worse — a `None` that travels several API calls
before failing somewhere unrelated.

tools/config.json is written by tools/setup.py and is gitignored: it describes
one deployment in one tenant. tools/config.example.json documents every key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
CONFIG_PATH = TOOLS_DIR / "config.json"


class ConfigError(SystemExit):
    """Exit with a message an operator can act on, not a stack trace."""

    def __init__(self, message: str):
        super().__init__(f"\nconfig error: {message}\n")


def load() -> Dict[str, Any]:
    """Read tools/config.json.

    utf-8-sig, not utf-8: PowerShell's Out-File writes a BOM, which makes
    json.loads fail with an opaque 'Expecting value: line 1 column 1' that
    reads like a corrupt file rather than an encoding problem.
    """
    if not CONFIG_PATH.exists():
        raise ConfigError(
            f"{CONFIG_PATH} does not exist.\n"
            "  Run `python tools/setup.py` to provision a deployment and write it,\n"
            "  or copy tools/config.example.json and fill it in by hand."
        )
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{CONFIG_PATH} is not valid JSON: {exc}") from None


def save(cfg: Dict[str, Any]) -> None:
    """Write config.json without a BOM. See load() for why that matters."""
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


_PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


def require(cfg: Dict[str, Any], *keys: str, hint: str = "") -> List[Any]:
    """Return the named values, failing clearly if any is absent or a placeholder."""
    missing = [
        k for k in keys
        if not cfg.get(k) or str(cfg.get(k)).lower() == _PLACEHOLDER
    ]
    if missing:
        raise ConfigError(
            f"tools/config.json is missing: {', '.join(missing)}"
            + (f"\n  {hint}" if hint else "")
        )
    return [cfg[k] for k in keys]


def demo(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the optional demo sandbox, with the principals normalised.

    The demo is deliberately a separate workspace from the data: lh_onesafe maps
    every weak point in the tenant, and naming ordinary users in data access
    roles on it is the wrong default for a security tool.
    """
    require(
        cfg, "demoWorkspaceId", "demoLakehouseId",
        hint="Run `python tools/setup.py --with-demo` to create the demo sandbox.",
    )

    principals = cfg.get("demoPrincipals") or []
    if len(principals) < 2:
        raise ConfigError(
            "demoPrincipals needs at least 2 entries — the demo exists to show a\n"
            "  multi-member role and a principal in several roles, which one user\n"
            "  cannot demonstrate.\n"
            '  Format: [{"objectId": "<entra-guid>", "upn": "user@tenant"}, ...]'
        )
    for i, p in enumerate(principals):
        if not isinstance(p, dict) or not p.get("objectId") or not p.get("upn"):
            raise ConfigError(
                f"demoPrincipals[{i}] must be an object with 'objectId' and 'upn'."
            )

    return {
        "workspaceId": cfg["demoWorkspaceId"],
        "workspaceName": cfg.get("demoWorkspaceName") or "OneSafe Demo",
        "lakehouseId": cfg["demoLakehouseId"],
        "lakehouseName": cfg.get("demoLakehouseName") or "lh_onesafe_demo",
        "semanticModelId": cfg.get("demoSemanticModelId") or "",
        "principals": principals,
        "objectIds": [p["objectId"] for p in principals],
        "upns": [p["upn"] for p in principals],
    }
