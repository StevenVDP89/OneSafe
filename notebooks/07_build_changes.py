# CELL ********************

# OneSafe :: 07_build_changes
# Day-over-day permission drift: what was granted, revoked or elevated.

# CELL ********************

# MAGIC %run 00_common

# CELL ********************

from pyspark.sql import functions as F

ensure_schemas()

summary = spark.table(f"{GOLD}.fact_access_summary")
snapshots = sorted(r.snapshot_date for r in summary.select("snapshot_date").distinct().collect())

if len(snapshots) < 2:
    print("[onesafe] only one snapshot available - no change detection yet")
    empty = spark.createDataFrame(
        [],
        "snapshot_date string, prev_snapshot_date string, principal_id string, "
        "item_id string, workspace_id string, item_type string, change_type string, "
        "prev_permission_level int, new_permission_level int, "
        "prev_permission_name string, new_permission_name string, access_path string",
    )
    save_table(empty, GOLD, "fact_access_change")
    log_run("build_changes", "Skipped", 0, "single snapshot")
else:
    current_date = snapshots[-1]
    previous_date = snapshots[-2]
    print(f"[onesafe] diffing {previous_date} -> {current_date}")

    keys = ["principal_id", "item_id"]

    cur = summary.where(F.col("snapshot_date") == current_date).select(
        *keys,
        "workspace_id",
        "item_type",
        F.col("max_permission_level").alias("new_permission_level"),
        F.col("max_permission_name").alias("new_permission_name"),
        F.col("primary_access_path").alias("access_path"),
    )
    prev = summary.where(F.col("snapshot_date") == previous_date).select(
        *keys,
        F.col("workspace_id").alias("p_workspace_id"),
        F.col("item_type").alias("p_item_type"),
        F.col("max_permission_level").alias("prev_permission_level"),
        F.col("max_permission_name").alias("prev_permission_name"),
        F.col("primary_access_path").alias("prev_access_path"),
    )

    joined = cur.join(prev, keys, "full_outer")

    changes = (
        joined.withColumn(
            "change_type",
            F.when(F.col("prev_permission_level").isNull(), F.lit("Granted"))
            .when(F.col("new_permission_level").isNull(), F.lit("Revoked"))
            .when(F.col("new_permission_level") > F.col("prev_permission_level"), F.lit("Elevated"))
            .when(F.col("new_permission_level") < F.col("prev_permission_level"), F.lit("Reduced"))
            .otherwise(F.lit("Unchanged")),
        )
        .where(F.col("change_type") != "Unchanged")
        .select(
            F.lit(current_date).alias("snapshot_date"),
            F.lit(previous_date).alias("prev_snapshot_date"),
            "principal_id",
            "item_id",
            F.coalesce(F.col("workspace_id"), F.col("p_workspace_id")).alias("workspace_id"),
            F.coalesce(F.col("item_type"), F.col("p_item_type")).alias("item_type"),
            "change_type",
            F.coalesce(F.col("prev_permission_level"), F.lit(0)).alias("prev_permission_level"),
            F.coalesce(F.col("new_permission_level"), F.lit(0)).alias("new_permission_level"),
            F.coalesce(F.col("prev_permission_name"), F.lit("None")).alias("prev_permission_name"),
            F.coalesce(F.col("new_permission_name"), F.lit("None")).alias("new_permission_name"),
            F.coalesce(F.col("access_path"), F.col("prev_access_path")).alias("access_path"),
        )
    )

    # Change history accumulates rather than being replaced, so a re-run of the
    # same day must first clear that day's rows.
    full = f"{GOLD}.fact_access_change"
    if spark.catalog.tableExists(full):
        spark.sql(f"DELETE FROM {full} WHERE snapshot_date = '{current_date}'")
        changes.write.format("delta").mode("append").saveAsTable(full)
    else:
        changes.write.format("delta").mode("overwrite").option(
            "overwriteSchema", "true"
        ).partitionBy("snapshot_date").saveAsTable(full)

    counts = changes.groupBy("change_type").count().collect()
    detail = ", ".join(f"{r['change_type']}={r['count']}" for r in counts)
    log_run("build_changes", "Succeeded", changes.count(), detail)
    print(f"[onesafe] changes: {detail}")
