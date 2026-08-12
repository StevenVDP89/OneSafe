# CELL ********************

# OneSafe :: 10_on_failure
# Runs only when an upstream pipeline activity fails.
#
# A failed unattended run is worthless if nobody can tell *what* failed, so this
# notebook turns the run log into a readable incident summary, publishes the
# telemetry table anyway (so the app shows the outage rather than stale-but-green
# data), and optionally posts to a webhook.

# CELL ********************

# MAGIC %run 00_common

# CELL ********************

from pyspark.sql import functions as F

FAILED_ACTIVITY = ""      # set by the pipeline via a parameter cell
FAILED_MESSAGE = ""

# CELL ********************

# Always refresh telemetry: the whole point is that a failed run is visible.
rows = build_pipeline_run_table()
print(f"[onesafe] telemetry rebuilt ({rows} rows)")

runs = (
    spark.table(f"{GOLD}.fact_pipeline_run")
    .where(F.col("snapshot_date") == SNAPSHOT_DATE)
    .orderBy("step_order")
)

problems = runs.where(~F.col("is_healthy")).collect()
ok = runs.where(F.col("is_healthy")).count()

lines = [
    f"OneSafe daily refresh FAILED for {SNAPSHOT_DATE}",
    f"  healthy steps : {ok}",
    f"  failing steps : {len(problems)}",
]
if FAILED_ACTIVITY:
    lines.append(f"  pipeline activity: {FAILED_ACTIVITY}")
if FAILED_MESSAGE:
    lines.append(f"  pipeline error   : {FAILED_MESSAGE[:500]}")
for p in problems:
    lines.append(f"  - {p['step']}: {p['status']} :: {(p['detail'] or '')[:300]}")
lines.append(
    "  tracebacks: Files/diag/error_<notebook>.txt in lh_onesafe"
)

report = "\n".join(lines)
print(report)

log_run("10_on_failure", "Success", records=len(problems), detail=report[:2000])

# CELL ********************

# Optional outbound notification. Kept opt-in via config so the pipeline has no
# hard dependency on a connector or gateway.
webhook = CONFIG.get("alertWebhookUrl")
if webhook:
    try:
        requests.post(webhook, json={"text": report}, timeout=60)
        print("[onesafe] alert posted to webhook")
    except Exception as exc:  # noqa: BLE001
        print(f"[onesafe] webhook post failed: {exc}")
else:
    print("[onesafe] no alertWebhookUrl configured, skipping webhook")

# CELL ********************

# Fail loudly so the pipeline run itself is marked failed in the monitoring hub.
raise RuntimeError(report[:1500])
