# CELL ********************

# OneSafe :: 08_validate
# Reconcile the computed effective access against Fabric's own answer.
#
# GET /v1/admin/users/{id}/access is the ground truth but is capped at 200
# requests/hour, so a bounded random sample is checked each day. Accuracy is
# published as a table so drift in the resolution logic surfaces immediately.

# CELL ********************

# MAGIC %run 00_common

# CELL ********************

from pyspark.sql import functions as F

SAMPLE_SIZE = 25   # well inside the 200/hour ceiling

summary = spark.table(f"{GOLD}.fact_access_summary").where(
    F.col("snapshot_date") == SNAPSHOT_DATE
)
principals = spark.table(f"{GOLD}.dim_principal").where(
    F.col("last_seen_date") == SNAPSHOT_DATE
)

# Only real, resolvable users can be checked through this API.
candidates = (
    principals.where(
        (F.col("principal_type") == "User")
        & F.col("is_resolved")
        & F.col("upn").isNotNull()
    )
    .select("principal_id", "upn")
    .distinct()
)

sample = candidates.orderBy(F.rand(seed=42)).limit(SAMPLE_SIZE).collect()
print(f"[onesafe] validating {len(sample)} principals")

# CELL ********************

results = []

for row in sample:
    pid = row["principal_id"]
    try:
        entities = paged_get(
            f"{FABRIC_API}/v1/admin/users/{pid}/access",
            RES_FABRIC,
            collection="accessEntities",
            tolerate=(400, 403, 404),
        )
    except ApiError as exc:
        results.append(
            {
                "principal_id": pid,
                "upn": row["upn"],
                "api_item_count": None,
                "model_item_count": None,
                "matched_count": None,
                "coverage_pct": None,
                "status": f"error: {str(exc)[:200]}",
            }
        )
        continue

    api_ids = {str(e.get("id")).lower() for e in (entities or []) if e.get("id")}
    model_ids = {
        r["item_id"]
        for r in summary.where(F.col("principal_id") == pid).select("item_id").collect()
    }

    matched = len(api_ids & model_ids)
    coverage = round(100.0 * matched / len(api_ids), 2) if api_ids else None

    results.append(
        {
            "principal_id": pid,
            "upn": row["upn"],
            "api_item_count": len(api_ids),
            "model_item_count": len(model_ids),
            "matched_count": matched,
            "coverage_pct": coverage,
            "status": "ok",
        }
    )
    # Stay comfortably below the documented rate limit.
    time.sleep(1.5)

# CELL ********************

schema = (
    "principal_id string, upn string, api_item_count int, model_item_count int, "
    "matched_count int, coverage_pct double, status string"
)
df = with_snapshot(spark.createDataFrame(results, schema))
append_snapshot(df, GOLD, "fact_validation")

scored = [r for r in results if r.get("coverage_pct") is not None]
avg = round(sum(r["coverage_pct"] for r in scored) / len(scored), 2) if scored else None

log_run(
    "validate",
    "Succeeded",
    len(results),
    f"avg coverage {avg}% across {len(scored)} principals",
)
print(f"[onesafe] validation complete - average coverage {avg}%")
