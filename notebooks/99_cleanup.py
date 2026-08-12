# CELL ********************

# OneSafe :: 99_cleanup
# Ad-hoc maintenance. Not part of the daily pipeline - run by hand when a
# synthetic or mistaken run_log entry needs removing and telemetry rebuilding.

# CELL ********************

# MAGIC %run 00_common

# CELL ********************

# Drop the row left by testing the on-failure handler directly. It logged a
# genuine Success, but showing a failure-handler step in the health pane implies
# an outage that never happened.
spark.sql(f"DELETE FROM {BRONZE}.run_log WHERE step = '10_on_failure'")

rows = build_pipeline_run_table()
print(f"[onesafe] telemetry rebuilt ({rows} rows)")

steps = (
    spark.table(f"{GOLD}.fact_pipeline_run")
    .where(f"snapshot_date = '{SNAPSHOT_DATE}'")
    .select("step", "status")
    .orderBy("step_order")
    .collect()
)
for s in steps:
    print(f"  {s['step']}: {s['status']}")
