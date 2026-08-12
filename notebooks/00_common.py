# CELL ********************

# OneSafe :: 00_common
# Shared auth, REST helpers, config and lakehouse utilities.
# Include from any OneSafe notebook with:  %run 00_common

import json
import time
import datetime as _dt
from typing import Any, Dict, Iterable, List, Optional

import requests

# CELL ********************

# ---------------------------------------------------------------- configuration

ONESAFE_CONFIG_PATH = "Files/config/onesafe_config.json"

FABRIC_API = "https://api.fabric.microsoft.com"
POWERBI_API = "https://api.powerbi.com"
GRAPH_API = "https://graph.microsoft.com"

RES_FABRIC = "https://api.fabric.microsoft.com"
RES_POWERBI = "https://analysis.windows.net/powerbi/api"
RES_GRAPH = "https://graph.microsoft.com"

BRONZE = "bronze"
SILVER = "silver"
GOLD = "gold"


def _load_config() -> Dict[str, Any]:
    """Read the OneSafe config from the attached lakehouse Files area.

    Falls back to an empty dict so notebooks can still run under the caller's
    own identity when no service principal has been provisioned.
    """
    try:
        import notebookutils  # noqa: F401

        raw = notebookutils.fs.head(f"{ONESAFE_CONFIG_PATH}", 1024 * 64)
        return json.loads(raw)
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"[onesafe] config not loaded ({exc}); falling back to caller identity")
        return {}


CONFIG = _load_config()

SNAPSHOT_DATE = _dt.datetime.utcnow().strftime("%Y-%m-%d")
SNAPSHOT_TS = _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

print(f"[onesafe] snapshot_date={SNAPSHOT_DATE}")

# CELL ********************

# ---------------------------------------------------------------- authentication

_TOKEN_CACHE: Dict[str, Any] = {}


def _client_credentials_token(resource: str) -> Optional[str]:
    tenant = CONFIG.get("tenantId")
    client_id = CONFIG.get("clientId")
    secret = CONFIG.get("clientSecret")
    if not (tenant and client_id and secret):
        return None
    resp = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "scope": f"{resource}/.default",
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _caller_token(resource: str) -> Optional[str]:
    """Fallback: use the identity executing the notebook."""
    try:
        import notebookutils

        audience = {
            RES_FABRIC: "pbi",
            RES_POWERBI: "pbi",
            RES_GRAPH: "graph",
        }.get(resource, "pbi")
        return notebookutils.credentials.getToken(audience)
    except Exception as exc:  # pragma: no cover
        print(f"[onesafe] caller token failed for {resource}: {exc}")
        return None


def get_token(resource: str, identity: str = "auto") -> str:
    """Return a bearer token for ``resource``.

    ``identity`` selects which principal to use:
      ``auto``   - service principal, falling back to the executing identity
      ``spn``    - service principal only
      ``caller`` - the identity running the notebook only

    Extraction runs on the service principal so the pipeline stays unattended.
    A few Power BI endpoints refuse service principals outright - the tenant
    setting governing them is separate from the read-only admin APIs - so those
    callers ask for ``caller`` explicitly rather than failing the run.

    Tokens are cached until 5 minutes before expiry.
    """
    cache_key = f"{identity}:{resource}"
    entry = _TOKEN_CACHE.get(cache_key)
    if entry and entry["expires"] > time.time() + 300:
        return entry["token"]

    if identity == "spn":
        token = _client_credentials_token(resource)
    elif identity == "caller":
        token = _caller_token(resource)
    else:
        token = _client_credentials_token(resource) or _caller_token(resource)

    if not token:
        raise RuntimeError(
            f"Unable to acquire a '{identity}' token for {resource}. Provide a service "
            f"principal in {ONESAFE_CONFIG_PATH} or run the notebook as a Fabric admin."
        )
    _TOKEN_CACHE[cache_key] = {"token": token, "expires": time.time() + 3300}
    return token


def auth_header(resource: str, identity: str = "auto") -> Dict[str, str]:
    return {"Authorization": f"Bearer {get_token(resource, identity)}"}

# CELL ********************

# ---------------------------------------------------------------- REST plumbing

class ApiError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} for {url}: {body[:500]}")
        self.status = status
        self.url = url
        self.body = body


# Statuses worth retrying: throttling plus transient gateway failures.
_RETRY_STATUS = {429, 500, 502, 503, 504}


def api_request(
    method: str,
    url: str,
    resource: str,
    *,
    json_body: Optional[Any] = None,
    params: Optional[Dict[str, Any]] = None,
    max_retries: int = 6,
    timeout: int = 300,
    tolerate: Iterable[int] = (),
    identity: str = "auto",
) -> Optional[Any]:
    """Single REST call with token refresh, throttling and backoff handling.

    ``tolerate`` lists status codes that should return ``None`` instead of raising
    (used for permission gaps we want to record rather than crash on).
    ``identity`` is passed through to :func:`get_token`.
    """
    tolerate = set(tolerate)
    delay = 5.0

    for attempt in range(max_retries + 1):
        headers = auth_header(resource, identity)
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        resp = requests.request(
            method, url, headers=headers, json=json_body, params=params, timeout=timeout
        )

        if resp.status_code in tolerate:
            return None

        if resp.status_code == 401 and attempt < max_retries:
            _TOKEN_CACHE.pop(f"{identity}:{resource}", None)
            time.sleep(2)
            continue

        if resp.status_code in _RETRY_STATUS and attempt < max_retries:
            wait = float(resp.headers.get("Retry-After", delay))
            # Guard against pathological Retry-After values.
            wait = min(max(wait, 1.0), 900.0)
            print(f"[onesafe] {resp.status_code} on {url} -> sleeping {wait:.0f}s")
            time.sleep(wait)
            delay = min(delay * 2, 300)
            continue

        if resp.status_code >= 400:
            raise ApiError(resp.status_code, url, resp.text)

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    raise ApiError(resp.status_code, url, resp.text)


def api_get(url: str, resource: str, **kwargs) -> Optional[Any]:
    return api_request("GET", url, resource, **kwargs)


def api_post(url: str, resource: str, **kwargs) -> Optional[Any]:
    return api_request("POST", url, resource, **kwargs)


def paged_get(
    url: str,
    resource: str,
    *,
    collection: str,
    params: Optional[Dict[str, Any]] = None,
    tolerate: Iterable[int] = (),
) -> List[Any]:
    """Follow Fabric ``continuationToken`` pagination and return all records."""
    out: List[Any] = []
    params = dict(params or {})
    seen_tokens = set()

    while True:
        page = api_get(url, resource, params=params, tolerate=tolerate)
        if page is None:
            break
        out.extend(page.get(collection) or [])

        token = page.get("continuationToken")
        # A repeated token means the service is looping; stop rather than spin.
        if not token or token in seen_tokens:
            break
        seen_tokens.add(token)
        params["continuationToken"] = token

    return out


def skip_top_get(
    url: str,
    resource: str,
    *,
    collection: str,
    page_size: int = 5000,
    params: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    """Follow ``$skip``/``$top`` pagination used by some admin endpoints."""
    out: List[Any] = []
    skip = 0
    while True:
        p = dict(params or {})
        p["$top"] = page_size
        p["$skip"] = skip
        page = api_get(url, resource, params=p)
        batch = (page or {}).get(collection) or []
        out.extend(batch)
        if len(batch) < page_size:
            break
        skip += page_size
    return out

# CELL ********************

# ---------------------------------------------------------------- lakehouse I/O

from pyspark.sql import SparkSession  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402

spark = SparkSession.builder.getOrCreate()

# Snapshot replacement is handled explicitly (DELETE + append) rather than via
# dynamic partition overwrite, which is incompatible with schema evolution.
spark.conf.set("spark.sql.sources.partitionOverwriteMode", "static")
spark.conf.set("spark.sql.parquet.vorder.enabled", "true")


def ensure_schemas() -> None:
    for schema in (BRONZE, SILVER, GOLD):
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def write_bronze(name: str, records: List[Any]) -> int:
    """Persist raw API payloads as newline-delimited JSON, partitioned by snapshot."""
    import notebookutils

    path = f"Files/bronze/{name}/snapshot_date={SNAPSHOT_DATE}/data.jsonl"
    payload = "\n".join(json.dumps(r, default=str) for r in records)
    notebookutils.fs.mkdirs(f"Files/bronze/{name}/snapshot_date={SNAPSHOT_DATE}")
    notebookutils.fs.put(path, payload, True)
    print(f"[onesafe] bronze/{name}: {len(records)} records -> {path}")
    return len(records)


def read_bronze(name: str, snapshot_date: Optional[str] = None) -> List[Any]:
    """Read a bronze JSONL payload back into Python objects.

    Spark is used rather than ``fs.head`` because scanner payloads routinely
    exceed the head buffer, which would silently truncate the JSON.
    """
    import notebookutils

    snap = snapshot_date or SNAPSHOT_DATE
    path = f"Files/bronze/{name}/snapshot_date={snap}/data.jsonl"
    try:
        info = notebookutils.fs.ls(path)[0]
    except Exception:
        print(f"[onesafe] bronze/{name} missing for {snap}")
        return []

    if info.size == 0:
        return []

    rows = (
        spark.read.option("wholetext", "false").text(info.path).collect()
    )
    out = []
    for r in rows:
        line = r["value"]
        if line and line.strip():
            out.append(json.loads(line))
    return out


def save_table(df, schema: str, name: str, *, partition_by: Optional[str] = "snapshot_date") -> None:
    """Write a Delta table, replacing only the current snapshot partition."""
    writer = df.write.format("delta").mode("overwrite").option("overwriteSchema", "true")
    if partition_by and partition_by in df.columns:
        writer = writer.partitionBy(partition_by)
    writer.saveAsTable(f"{schema}.{name}")
    print(f"[onesafe] wrote {schema}.{name} ({df.count()} rows)")


def append_snapshot(df, schema: str, name: str) -> None:
    """Append the current snapshot, replacing it if the notebook is re-run."""
    full = f"{schema}.{name}"
    if spark.catalog.tableExists(full):
        spark.sql(f"DELETE FROM {full} WHERE snapshot_date = '{SNAPSHOT_DATE}'")
        # mergeSchema: these tables mirror evolving REST payloads, so a new
        # column should widen the table rather than fail the run.
        df.write.format("delta").mode("append").option(
            "mergeSchema", "true"
        ).saveAsTable(full)
    else:
        df.write.format("delta").mode("overwrite").option(
            "overwriteSchema", "true"
        ).partitionBy("snapshot_date").saveAsTable(full)
    print(f"[onesafe] appended snapshot {SNAPSHOT_DATE} to {full}")


def with_snapshot(df):
    return df.withColumn("snapshot_date", F.lit(SNAPSHOT_DATE)).withColumn(
        "snapshot_ts", F.lit(SNAPSHOT_TS)
    )


def log_run(step: str, status: str, records: int = 0, detail: str = "") -> None:
    """Record pipeline telemetry so failures are visible in the model."""
    row = [
        {
            "snapshot_date": SNAPSHOT_DATE,
            "snapshot_ts": SNAPSHOT_TS,
            "step": step,
            "status": status,
            "records": int(records),
            "detail": detail[:2000],
        }
    ]
    df = spark.createDataFrame(row)
    full = f"{BRONZE}.run_log"
    if spark.catalog.tableExists(full):
        df.write.format("delta").mode("append").saveAsTable(full)
    else:
        df.write.format("delta").mode("overwrite").option(
            "overwriteSchema", "true"
        ).saveAsTable(full)
    print(f"[onesafe] {step}: {status} ({records} records) {detail[:200]}")


# The steps the daily pipeline is expected to run, in order. Used to detect
# steps that never reported at all (a hard crash logs nothing).
PIPELINE_STEPS = [
    "01_extract_inventory",
    "02_extract_scanner",
    "03_extract_onelake",
    "04_extract_graph",
    "05_transform_silver",
    "06_build_gold",
    "07_build_changes",
    "08_validate",
    "09_refresh_sql_endpoint",
    "09_refresh_model",
]

# Notebooks log fine-grained, human-written step names ("extract_inventory.workspaces"),
# which is useful in the raw log but too granular for a health view. This maps the
# leading segment of a logged name onto the canonical pipeline step.
STEP_ALIASES = {
    "extract_inventory": "01_extract_inventory",
    "extract_scanner": "02_extract_scanner",
    "extract_onelake": "03_extract_onelake",
    "extract_graph": "04_extract_graph",
    "transform_silver": "05_transform_silver",
    "build_gold": "06_build_gold",
    "build_changes": "07_build_changes",
    "validate": "08_validate",
    "09_refresh_sql_endpoint": "09_refresh_sql_endpoint",
    "09_refresh_model": "09_refresh_model",
}

# Severity ordering so that one failing sub-step cannot be masked by a later
# success in the same notebook. Higher wins.
_STATUS_SEVERITY = {
    "success": 0, "succeeded": 0, "ok": 0,
    "skipped": 1,
    "warning": 2, "warn": 2, "partial": 2,
    "failed": 3, "failure": 3, "error": 3,
}


def build_pipeline_run_table() -> int:
    """Publish per-step run telemetry to gold so the app can show refresh health.

    Steps that produced no log row at all are emitted as ``NotRun`` rather than
    being silently absent, which is what makes a crashed step visible.
    """
    from pyspark.sql import functions as _F

    if not spark.catalog.tableExists(f"{BRONZE}.run_log"):
        return 0

    log = spark.table(f"{BRONZE}.run_log")

    # Canonical step = alias lookup on the segment before the first dot.
    root = _F.split(_F.col("step"), r"\.").getItem(0)
    canon = _F.create_map(
        *[x for k, v in STEP_ALIASES.items() for x in (_F.lit(k), _F.lit(v))]
    )[root]

    sev = _F.create_map(
        *[x for k, v in _STATUS_SEVERITY.items() for x in (_F.lit(k), _F.lit(v))]
    )[_F.lower(_F.col("status"))]

    tagged = (
        log.withColumn("canon_step", canon)
        # An unrecognised step name must not vanish; keep it under its own name
        # so a newly added notebook shows up rather than being silently dropped.
        .withColumn("canon_step", _F.coalesce(_F.col("canon_step"), root))
        .withColumn("severity", _F.coalesce(sev, _F.lit(2)))
    )

    # A step can be attempted more than once in a day (a rerun after fixing a
    # failure). The operator cares about where the step ended up, so the latest
    # attempt supersedes earlier ones - otherwise a successful retry would be
    # permanently masked by the failure it fixed. Sub-steps within a single
    # notebook run share a snapshot_ts, so worst-of-run is still preserved.
    latest = tagged.groupBy("snapshot_date", "canon_step").agg(
        _F.max("snapshot_ts").alias("latest_ts")
    )
    tagged = (
        tagged.join(latest, ["snapshot_date", "canon_step"])
        .where(_F.col("snapshot_ts") == _F.col("latest_ts"))
        .drop("latest_ts")
    )

    # Worst status within the latest attempt wins; records sum across sub-steps.
    agg = tagged.groupBy("snapshot_date", "canon_step").agg(
        _F.max("severity").alias("severity"),
        _F.sum("records").alias("records"),
        _F.max("snapshot_ts").alias("snapshot_ts"),
        _F.concat_ws(
            " | ",
            _F.collect_list(
                _F.when(
                    (_F.col("detail").isNotNull()) & (_F.length("detail") > 0),
                    _F.concat_ws(": ", _F.col("step"), _F.col("detail")),
                )
            ),
        ).alias("detail"),
    ).withColumnRenamed("canon_step", "step")

    dates = log.select("snapshot_date").distinct()
    expected = spark.createDataFrame(
        [(s, i) for i, s in enumerate(PIPELINE_STEPS)], ["step", "step_order"]
    )
    grid = dates.crossJoin(expected)

    # Union in any non-canonical steps that were logged, so nothing is lost.
    extra = (
        agg.join(expected, ["step"], "left_anti")
        .select("snapshot_date", "step")
        .distinct()
        .withColumn("step_order", _F.lit(len(PIPELINE_STEPS)))
    )
    grid = grid.unionByName(extra)

    status_from_sev = (
        _F.when(_F.col("severity") == 0, _F.lit("Success"))
        .when(_F.col("severity") == 1, _F.lit("Skipped"))
        .when(_F.col("severity") == 2, _F.lit("Warning"))
        .otherwise(_F.lit("Failed"))
    )

    runs = (
        grid.join(agg, ["snapshot_date", "step"], "left")
        .withColumn(
            "status",
            _F.when(_F.col("severity").isNull(), _F.lit("NotRun")).otherwise(
                status_from_sev
            ),
        )
        .withColumn("records", _F.coalesce(_F.col("records"), _F.lit(0)).cast("int"))
        .withColumn("detail", _F.coalesce(_F.col("detail"), _F.lit("")))
        .withColumn("snapshot_ts", _F.coalesce(_F.col("snapshot_ts"), _F.lit(SNAPSHOT_TS)))
        .withColumn("is_healthy", _F.col("status").isin("Success", "Skipped", "Warning"))
        .select(
            "snapshot_date", "snapshot_ts", "step", "step_order",
            "status", "records", "detail", "is_healthy",
        )
    )

    save_table(runs, GOLD, "fact_pipeline_run")
    return runs.count()


# ---------------------------------------------------------------- diagnostics

NOTEBOOK_NAME = "unknown"


def _install_error_trap() -> None:
    """Persist any uncaught cell exception to the lakehouse.

    Fabric job runs surface only a generic "statements failed" message, so
    without this an unattended failure gives no actionable detail.
    """
    try:
        shell = get_ipython()  # noqa: F821
    except Exception:
        return

    def _handler(sh, etype, evalue, tb, tb_offset=None):
        try:
            import traceback as _tb
            import notebookutils

            text = "".join(_tb.format_exception(etype, evalue, tb))
            notebookutils.fs.mkdirs("Files/diag")
            notebookutils.fs.put(f"Files/diag/error_{NOTEBOOK_NAME}.txt", text, True)
        except Exception:
            pass
        return sh.showtraceback((etype, evalue, tb), tb_offset=tb_offset)

    try:
        shell.set_custom_exc((Exception,), _handler)
    except Exception:
        pass


_install_error_trap()

print("[onesafe] common module loaded")
