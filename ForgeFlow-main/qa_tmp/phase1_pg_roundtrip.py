"""QA Phase 1 — real-PG structural round-trip for experiences B4 columns.

Independent verification. Does NOT import the app; talks straight to PG via
psycopg so the evidence is third-party to any engineer's test harness.
"""
import json
import sys
import uuid

import psycopg

DSN = "postgresql://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"
FOUR = ["conflict_with", "merged_from", "dedup_key", "confidence"]

def main() -> int:
    results = {}
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            # --- external物证 1: alembic_version -------------------------
            cur.execute("SELECT version_num FROM alembic_version")
            results["alembic_version"] = [r[0] for r in cur.fetchall()]

            # --- external物证 2: information_schema.columns --------------
            cur.execute(
                """
                SELECT column_name, data_type, udt_name, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name = 'experiences'
                  AND column_name = ANY(%s)
                ORDER BY column_name
                """,
                (FOUR,),
            )
            results["columns"] = [
                {
                    "column": r[0],
                    "data_type": r[1],
                    "udt_name": r[2],
                    "is_nullable": r[3],
                    "default": r[4],
                }
                for r in cur.fetchall()
            ]

            # --- B4 round-trip: INSERT -> SELECT -------------------------
            cw = [str(uuid.uuid4()), str(uuid.uuid4())]
            mf = [str(uuid.uuid4())]
            dk = "qa-rt-" + uuid.uuid4().hex
            conf = 0.87
            cur.execute(
                """
                INSERT INTO experiences
                    (summary, conflict_with, merged_from, dedup_key, confidence)
                VALUES (%s, %s::uuid[], %s::uuid[], %s, %s)
                RETURNING id
                """,
                ("qa round-trip row", cw, mf, dk, conf),
            )
            row_id = cur.fetchone()[0]
            cur.execute(
                """
                SELECT conflict_with, merged_from, dedup_key, confidence
                FROM experiences WHERE id = %s
                """,
                (row_id,),
            )
            got = cur.fetchone()
            results["roundtrip"] = {
                "id": str(row_id),
                "sent_conflict_with": cw,
                "got_conflict_with": [str(x) for x in got[0]],
                "sent_merged_from": mf,
                "got_merged_from": [str(x) for x in got[1]],
                "sent_dedup_key": dk,
                "got_dedup_key": got[2],
                "sent_confidence": conf,
                "got_confidence": got[3],
            }
            conn.rollback()  # leave no residue

    # --- assertions -----------------------------------------------------------
    ok = True
    cols = {c["column"] for c in results["columns"]}
    missing = [c for c in FOUR if c not in cols]
    if missing:
        ok = False
        results["fail_reason"] = f"missing columns: {missing}"

    rt = results.get("roundtrip", {})
    checks = {
        "conflict_with": rt.get("sent_conflict_with") == rt.get("got_conflict_with"),
        "merged_from": rt.get("sent_merged_from") == rt.get("got_merged_from"),
        "dedup_key": rt.get("sent_dedup_key") == rt.get("got_dedup_key"),
        "confidence": rt.get("sent_confidence") == rt.get("got_confidence"),
    }
    results["checks"] = checks
    if not all(checks.values()):
        ok = False
    results["verdict"] = "PASSED" if ok else "FAILED"

    print("=== PHASE1_PG_ROUNDTRIP ===")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    print("=== VERDICT:", results["verdict"], "===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
