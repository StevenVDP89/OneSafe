# CELL ********************

# OneSafe :: 97_seed_demo_lakehouse
# Creates the small demo tables that the OneLake Security demo roles scope to.
#
# Why this is needed
# ------------------
# OneLake row- and column-level constraints are validated server-side against
# the real table schema: a rule naming a column that does not exist, or a row
# predicate over a table that does not exist, is rejected with
# `InvalidRLSPredicate`. So the demo lakehouse cannot stay empty - the tables
# have to exist before the roles can reference them.
#
# Run on demand, not in the daily pipeline:
#   python tools/run_notebooks.py 97_seed_demo_lakehouse

# CELL ********************

%run 00_common

# CELL ********************

# Demo sandbox ids come from the runtime config in OneLake, so this notebook
# carries no tenant-specific values.
DEMO_WORKSPACE_ID = CONFIG.get("demoWorkspaceId")
DEMO_LAKEHOUSE_ID = CONFIG.get("demoLakehouseId")

if not DEMO_WORKSPACE_ID or not DEMO_LAKEHOUSE_ID:
    raise SystemExit(
        "demoWorkspaceId and demoLakehouseId must be present in the runtime config.\n"
        "Run `python tools/setup.py --with-demo` then `python tools/upload_config.py --sync`."
    )

BASE = (
    f"abfss://{DEMO_WORKSPACE_ID}@onelake.dfs.fabric.microsoft.com/"
    f"{DEMO_LAKEHOUSE_ID}/Tables/dbo"
)

# CELL ********************

from pyspark.sql import Row


def write(name, rows):
    df = spark.createDataFrame(rows)
    path = f"{BASE}/{name}"
    df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(path)
    print(f"[onesafe] wrote {name}: {df.count()} rows, columns={df.columns}")


write("sales", [
    Row(SaleId=1, Region="EMEA", Country="Belgium", Customer="Contoso NV", Amount=128400.0),
    Row(SaleId=2, Region="EMEA", Country="Germany", Customer="Fabrikam GmbH", Amount=94250.0),
    Row(SaleId=3, Region="AMER", Country="United States", Customer="Adventure Works", Amount=211900.0),
    Row(SaleId=4, Region="APAC", Country="Japan", Customer="Tailspin KK", Amount=154300.0),
])

write("sentiment", [
    Row(FeedbackId=1, Region="EMEA", Score=0.82, Comment="Fast onboarding"),
    Row(FeedbackId=2, Region="AMER", Score=0.41, Comment="Reporting is slow"),
    Row(FeedbackId=3, Region="APAC", Score=0.67, Comment="Good support"),
])

write("reference", [
    Row(Code="EMEA", Description="Europe, Middle East and Africa"),
    Row(Code="AMER", Description="Americas"),
    Row(Code="APAC", Description="Asia Pacific"),
])

# Carries deliberate PII columns so column-level security has something worth
# hiding rather than an arbitrary restriction.
write("customer", [
    Row(CustomerId=1, CustomerName="Contoso NV", Region="EMEA", Segment="Enterprise",
        TaxId="BE0123456789", ContactEmail="ap@contoso.example"),
    Row(CustomerId=2, CustomerName="Fabrikam GmbH", Region="EMEA", Segment="Enterprise",
        TaxId="DE811234567", ContactEmail="finance@fabrikam.example"),
    Row(CustomerId=3, CustomerName="Adventure Works", Region="AMER", Segment="Corporate",
        TaxId="US-94-1234567", ContactEmail="ap@adventure.example"),
    Row(CustomerId=4, CustomerName="Tailspin KK", Region="APAC", Segment="SMB",
        TaxId="JP-1234567890", ContactEmail="keiri@tailspin.example"),
])

write("transactions", [
    Row(TransactionId=1, Region="EMEA", Amount=12400.0, CardNumber="4111-1111-1111-1111",
        AccountIban="BE68539007547034"),
    Row(TransactionId=2, Region="AMER", Amount=88100.0, CardNumber="5222-1111-1111-1111",
        AccountIban="US64SVBKUS6S3300958879"),
    Row(TransactionId=3, Region="APAC", Amount=31900.0, CardNumber="6333-1111-1111-1111",
        AccountIban="JP12ABCD1234567890"),
])

# CELL ********************

for name in ["sales", "sentiment", "reference", "customer", "transactions"]:
    n = spark.read.format("delta").load(f"{BASE}/{name}").count()
    if n == 0:
        raise RuntimeError(f"{name} is empty - OneLake rules cannot validate against it")
print("[onesafe] demo lakehouse seeded")
