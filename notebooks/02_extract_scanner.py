# CELL ********************

# OneSafe :: 02_extract_scanner
# Power BI Scanner API - the primary source of per-item permissions,
# semantic model RLS roles and lineage.
#
# Documented limits: 500 requests/hour, 16 concurrent scans, 100 workspaces
# per scan. The batching and throttling below stays well inside them.

# CELL ********************

# MAGIC %run 00_common

# CELL ********************

import math

SCAN_BATCH_SIZE = 100
MAX_CONCURRENT_SCANS = 8       # half the documented ceiling, for safety margin
SCAN_POLL_SECONDS = 10
SCAN_POLL_MAX_MINUTES = 30

SCAN_PARAMS = {
    "lineage": "True",
    "datasourceDetails": "True",
    # Schema/expressions require the "detailed metadata" tenant settings and
    # inflate payloads massively. Security posture does not need them.
    "datasetSchema": "False",
    "datasetExpressions": "False",
    "getArtifactUsers": "True",
}

# CELL ********************

workspaces = read_bronze("workspaces")
if not workspaces:
    raise RuntimeError("No workspaces in bronze - run 01_extract_inventory first.")

# Deleted / orphaned workspaces are rejected by the scanner and fail the batch.
scannable = [
    w["id"]
    for w in workspaces
    if w.get("id") and (w.get("state") or "Active") == "Active"
]
print(f"[onesafe] {len(scannable)} scannable workspaces")

batches = [
    scannable[i : i + SCAN_BATCH_SIZE]
    for i in range(0, len(scannable), SCAN_BATCH_SIZE)
]
print(f"[onesafe] {len(batches)} scan batches of up to {SCAN_BATCH_SIZE}")

# CELL ********************

def submit_scan(batch):
    resp = api_post(
        f"{POWERBI_API}/v1.0/myorg/admin/workspaces/getInfo",
        RES_POWERBI,
        json_body={"workspaces": batch},
        params=SCAN_PARAMS,
    )
    return resp["id"]


def await_scan(scan_id):
    deadline = time.time() + SCAN_POLL_MAX_MINUTES * 60
    while time.time() < deadline:
        status = api_get(
            f"{POWERBI_API}/v1.0/myorg/admin/workspaces/scanStatus/{scan_id}",
            RES_POWERBI,
        )
        state = (status or {}).get("status")
        if state == "Succeeded":
            return True
        if state == "Failed":
            raise RuntimeError(f"Scan {scan_id} failed: {status}")
        time.sleep(SCAN_POLL_SECONDS)
    raise TimeoutError(f"Scan {scan_id} did not complete within {SCAN_POLL_MAX_MINUTES}m")


def fetch_scan_result(scan_id):
    return api_get(
        f"{POWERBI_API}/v1.0/myorg/admin/workspaces/scanResult/{scan_id}",
        RES_POWERBI,
    )

# CELL ********************

# Scans are submitted in waves so at most MAX_CONCURRENT_SCANS are ever in
# flight; each wave is drained before the next is submitted. A failed batch is
# isolated rather than aborting the whole extraction.

scanned_workspaces = []
failed_batches = []

for wave_start in range(0, len(batches), MAX_CONCURRENT_SCANS):
    wave = batches[wave_start : wave_start + MAX_CONCURRENT_SCANS]
    scan_ids = []

    for batch in wave:
        try:
            scan_ids.append((submit_scan(batch), batch))
        except Exception as exc:
            print(f"[onesafe] submit failed: {exc}")
            failed_batches.append({"batch": batch, "error": str(exc)[:400]})

    for scan_id, batch in scan_ids:
        try:
            await_scan(scan_id)
            result = fetch_scan_result(scan_id)
            scanned_workspaces.extend((result or {}).get("workspaces") or [])
        except Exception as exc:
            print(f"[onesafe] scan {scan_id} failed: {exc}")
            failed_batches.append({"batch": batch, "error": str(exc)[:400]})

    done = min(wave_start + MAX_CONCURRENT_SCANS, len(batches))
    print(f"[onesafe] scan waves {done}/{len(batches)} - {len(scanned_workspaces)} workspaces")

# CELL ********************

write_bronze("scan_workspaces", scanned_workspaces)
if failed_batches:
    write_bronze("scan_failures", failed_batches)

status = "Succeeded" if not failed_batches else "CompletedWithErrors"
log_run(
    "extract_scanner",
    status,
    len(scanned_workspaces),
    f"{len(failed_batches)} failed batches",
)
print(f"[onesafe] scanner complete: {len(scanned_workspaces)} workspaces, {len(failed_batches)} failures")
