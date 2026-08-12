# CELL ********************

# OneSafe :: 99_check_rls_members
# Diagnostic: where do semantic-model RLS role members actually show up?
#
# The definition API strips roles[].members[], so OneSafe reads membership from
# the scanner instead. This checks the scanner really reports members that were
# written over XMLA, and reads them back through TOM as ground truth.
#
# A notebook *job* produces no cell output, and a raised exception cancels the
# whole session before any later cell runs. So this is one cell, every probe is
# individually guarded, and the report is written in a finally block — a
# diagnostic that only reports when nothing is wrong is worthless.

# CELL ********************

%run 00_common

# CELL ********************

import io
import traceback

_buf = io.StringIO()


def report(*args):
    line = " ".join(str(a) for a in args)
    print(line)
    print(line, file=_buf)


def probe(title, fn):
    report(f"=== {title} ===")
    try:
        fn()
    except Exception:  # noqa: BLE001
        report(traceback.format_exc())
    report("")


DEMO_WORKSPACE = "OneSafe Demo"
DEMO_MODEL = "sm_onesafe_demo"
try:
    from pyspark.sql import functions as F

    def silver_members():
        df = spark.table(f"{SILVER}.rls_role_members")
        report("all snapshots:", df.count(), "| today:",
               df.filter(F.col("snapshot_date") == SNAPSHOT_DATE).count())
        for r in df.limit(50).collect():
            report("   ", r.asDict())

    probe("silver.rls_role_members", silver_members)

    def silver_models():
        df = spark.table(f"{SILVER}.items").filter(
            F.col("item_type") == "SemanticModel")
        report("semantic models in silver.items:", df.count())
        for r in df.filter(F.col("display_name").contains("onesafe_demo")).collect():
            report("   ", r.asDict())

    probe("silver.items (demo model present?)", silver_models)

    def scanner_payload():
        raw = read_bronze("scan_workspaces")
        report("bronze/scan_workspaces records:", len(raw))
        blob = json.dumps(raw)
        report("mentions rowLevelSecurity:", blob.count("rowLevelSecurity"))
        report("mentions sm_onesafe_demo:", blob.count(DEMO_MODEL))
        i = blob.find(DEMO_MODEL)
        report("context ->",
               blob[max(0, i - 400): i + 2500] if i >= 0
               else "(model not present in scan output at all)")

    probe("bronze scanner payload", scanner_payload)

    def tom_truth():
        # TOM is authoritative: the same surface the members were written through.
        import sempy.fabric as fabric

        server = fabric.create_tom_server(readonly=True, workspace=DEMO_WORKSPACE)
        db = server.Databases.GetByName(DEMO_MODEL)
        for role in db.Model.Roles:
            names = [f"{m.Name} ({m.MemberID})" for m in role.Members]
            report(f"{role.Name}: {len(names)} member(s) -> {names}")
        server.Disconnect()

    probe("TOM ground truth", tom_truth)
finally:
    try:
        notebookutils.fs.mkdirs("Files/diag")
    except Exception:  # noqa: BLE001
        pass
    notebookutils.fs.put("Files/diag/rls_check.txt", _buf.getvalue() or "(empty)", True)
    print("wrote Files/diag/rls_check.txt")
