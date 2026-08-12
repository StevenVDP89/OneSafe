# CELL ********************

# OneSafe :: 09_refresh_model
# Final pipeline step: make the freshly written gold tables visible to the SQL
# analytics endpoint, then refresh the Direct Lake semantic model.
#
# Two non-obvious things this handles:
#
#  1. Spark-created schemas/tables are invisible to the SQL analytics endpoint
#     until its metadata is explicitly synced. Skipping this makes DAX fail with
#     "Invalid object name 'gold.fact_effective_access'" even though the Delta
#     tables exist.
#  2. Direct Lake still needs a refresh to pick up new framing, and the refresh
#     is asynchronous, so we poll it to completion rather than fire-and-forget.
#     A pipeline step that returns before the model is actually usable is worse
#     than no step at all.

# CELL ********************

# MAGIC %run 00_common

# CELL ********************

import time

WORKSPACE_ID = CONFIG["workspaceId"]
SQL_ENDPOINT_ID = CONFIG.get("sqlEndpointId")
MODEL_ID = CONFIG.get("semanticModelId")

REFRESH_POLL_SECONDS = 20
REFRESH_TIMEOUT_SECONDS = 60 * 45

# CELL ********************

# ---------------------------------------------------------------- sync SQL endpoint


def refresh_sql_endpoint_metadata() -> str:
    """Force the SQL analytics endpoint to re-read Delta metadata from OneLake."""
    if not SQL_ENDPOINT_ID:
        return "skipped: no sqlEndpointId in config"

    url = (
        f"{FABRIC_API}/v1/workspaces/{WORKSPACE_ID}"
        f"/sqlEndpoints/{SQL_ENDPOINT_ID}/refreshMetadata"
    )
    result = api_post(
        url,
        RES_FABRIC,
        params={"preview": "true"},
        json_body={"timeout": {"timeUnit": "Minutes", "value": 15}},
    )

    # The API returns a per-table sync result; surface failures instead of
    # letting a silently unsynced table break the model refresh downstream.
    rows = result if isinstance(result, list) else (result or {}).get("value", [])
    failed = [
        r for r in rows
        if isinstance(r, dict) and r.get("status") not in (None, "Success", "NotRun")
    ]
    detail = f"{len(rows)} tables synced, {len(failed)} failed"
    if failed:
        detail += f" :: {failed[:5]}"
    print(f"[onesafe] sql endpoint metadata: {detail}")
    return detail


sql_detail = "not attempted"
try:
    sql_detail = refresh_sql_endpoint_metadata()
    # A skip is not a success. Reporting it as one would hide a misconfigured
    # pipeline behind a green dashboard, which is the failure mode this whole
    # telemetry table exists to prevent.
    log_run(
        "09_refresh_sql_endpoint",
        "Skipped" if sql_detail.startswith("skipped") else "Success",
        detail=sql_detail,
    )
except Exception as exc:  # noqa: BLE001
    # A metadata sync failure is worth recording but should not stop the refresh
    # attempt: the endpoint often catches up on its own.
    sql_detail = f"{type(exc).__name__}: {exc}"
    print(f"[onesafe] sql endpoint metadata failed: {sql_detail}")
    log_run("09_refresh_sql_endpoint", "Warning", detail=sql_detail)

# CELL ********************

# ---------------------------------------------------------------- refresh model

# The Power BI refresh endpoint is governed by a different tenant setting than
# the read-only admin APIs the rest of OneSafe uses, and in many tenants service
# principals are excluded from it ("API is not accessible for application").
# Rather than fail the pipeline, fall back to the identity executing the
# notebook, which in a scheduled run is the pipeline owner (a Fabric admin).
REFRESH_IDENTITY = "auto"


def _refresh_url(suffix: str = "") -> str:
    return f"{POWERBI_API}/v1.0/myorg/datasets/{MODEL_ID}/refreshes{suffix}"


def start_refresh() -> str:
    """Kick off the refresh, returning the identity that was accepted."""
    global REFRESH_IDENTITY
    body = {"type": "full", "commitMode": "transactional", "objects": []}
    try:
        api_post(_refresh_url(), RES_POWERBI, json_body=body, identity="spn")
        REFRESH_IDENTITY = "spn"
    except ApiError as exc:
        if exc.status not in (401, 403):
            raise
        print(f"[onesafe] service principal refused for refresh ({exc.status}), using caller identity")
        api_post(_refresh_url(), RES_POWERBI, json_body=body, identity="caller")
        REFRESH_IDENTITY = "caller"
    return REFRESH_IDENTITY


def wait_for_refresh() -> dict:
    deadline = time.time() + REFRESH_TIMEOUT_SECONDS
    last = {}
    while time.time() < deadline:
        time.sleep(REFRESH_POLL_SECONDS)
        history = api_get(
            _refresh_url(),
            RES_POWERBI,
            params={"$top": 1},
            identity=REFRESH_IDENTITY,
        )
        entries = (history or {}).get("value", [])
        if not entries:
            continue
        last = entries[0]
        status = last.get("status")
        print(f"[onesafe] model refresh: {status}")
        if status in ("Completed", "Failed", "Disabled"):
            return last
    raise TimeoutError(f"model refresh did not settle within {REFRESH_TIMEOUT_SECONDS}s")


if not MODEL_ID:
    print("[onesafe] no semanticModelId in config, skipping model refresh")
    log_run("09_refresh_model", "Skipped", detail="no semanticModelId")
else:
    try:
        used = start_refresh()
        outcome = wait_for_refresh()
        status = outcome.get("status")
        if status != "Completed":
            raise RuntimeError(
                f"refresh {status}: {outcome.get('serviceExceptionJson', '')[:500]}"
            )
        log_run("09_refresh_model", "Success", detail=f"identity: {used}; sql: {sql_detail}")
        print("[onesafe] semantic model refreshed")
    except Exception as exc:  # noqa: BLE001
        log_run("09_refresh_model", "Failed", detail=f"{type(exc).__name__}: {exc}")
        raise

# CELL ********************

# Publish per-step telemetry last, so this snapshot's own outcome is included.
rows = build_pipeline_run_table()

# ...but that write lands *after* the refresh above, so without a second pass the
# model would always report the previous run's health - the one thing this table
# exists to prevent. Re-frame just this table; it is tiny, so the cost is trivial
# compared with showing admins stale refresh status.
if MODEL_ID and rows:
    try:
        api_post(
            _refresh_url(),
            RES_POWERBI,
            json_body={
                "type": "full",
                "commitMode": "transactional",
                "objects": [{"table": "fact_pipeline_run"}],
            },
            identity=REFRESH_IDENTITY,
        )
        print("[onesafe] re-framed fact_pipeline_run so telemetry reflects this run")
    except Exception as exc:  # noqa: BLE001
        # Non-fatal: the table is correct in the lakehouse either way.
        print(f"[onesafe] telemetry re-frame failed (non-fatal): {exc}")

print(f"[onesafe] 09_refresh_model complete ({rows} telemetry rows)")
