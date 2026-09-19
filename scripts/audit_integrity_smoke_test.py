from __future__ import annotations

import gc
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "audit_integrity_smoke_test.sqlite3")
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_connection, reset_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.audit import record_audit, verify_audit_log_integrity  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def tamper_audit_row(audit_id: str) -> None:
    run_sql(
        """
        UPDATE audit_logs
        SET detail_json = '{"tampered":true}'
        WHERE id = ?
        """,
        (audit_id,),
    )


def run_sql(sql: str, params: tuple = ()) -> None:
    """Run one statement on a connection that is closed before we return.

    `with get_connection() as conn:` commits but never closes -- `__exit__` on a
    raw sqlite3 connection only ends the transaction. Two things then keep the
    file locked on Windows, and both break `reset_database`'s `unlink()`:

    * connections left in a reference cycle (every `with get_connection()` in
      `init_db` leaves one) are only reclaimed by the cycle collector, so a
      `gc.collect()` has to run before the next reset;
    * a `with ... as conn:` written directly in `main()` binds `conn` for the
      rest of the function, so the collector cannot reclaim it at all -- that
      one has to be closed explicitly, which is what this helper is for.
    """
    conn = get_connection()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def fresh_database() -> None:
    """`reset_database`, after clearing the connections that would block it."""
    gc.collect()
    reset_database(seed=True)


def hashed_rows() -> dict[str, dict]:
    """Three freshly chained rows on a clean database."""
    fresh_database()
    return {
        name: record_audit(f"audit.chain.{name}", "smoke", name, {}, actor="smoke")
        for name in ("a", "b", "c")
    }


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()

    first = record_audit("audit.integrity.first", "smoke", "first", {"step": 1}, actor="smoke")
    second = record_audit("audit.integrity.second", "smoke", "second", {"step": 2}, actor="smoke")
    assert first["row_hash"], first
    assert second["previous_hash"] == first["row_hash"], (first, second)

    initial = verify_audit_log_integrity()
    assert initial["valid"] is True, initial
    assert initial["checked_count"] >= 2, initial
    assert initial["tampered_count"] == 0, initial
    assert initial["broken_link_count"] == 0, initial

    # --- regression: clearing hashes must not read as "verified" ---
    # Before this, `UPDATE audit_logs SET row_hash = NULL` made every row look
    # like a pre-hashing legacy row, so the verifier returned valid=True with
    # checked_count=0: deleting the evidence was a complete bypass.
    # Runs before the TestClient block on purpose: on Windows the app keeps the
    # SQLite file open, and this section needs to reset the database.
    hashed_rows()
    assert verify_audit_log_integrity()["valid"] is True

    # (a) every hash cleared -> nothing left to verify, which is not a pass
    run_sql("UPDATE audit_logs SET row_hash = NULL")
    cleared = verify_audit_log_integrity()
    assert cleared["valid"] is False, cleared
    assert cleared["checked_count"] == 0, cleared

    # (b) a gap *after* the chain started is reported, not skipped as legacy
    rows = hashed_rows()
    run_sql("UPDATE audit_logs SET row_hash = NULL WHERE id = ?", (rows["c"]["id"],))
    gap = verify_audit_log_integrity()
    assert gap["valid"] is False, gap
    assert gap["unhashed_count"] == 1, gap
    # Relative to the seeded rows, which are hashed too: exactly the one row we
    # cleared is missing from the chain, and nothing is excused as legacy.
    assert gap["checked_count"] == gap["total_count"] - 1, gap
    assert gap["legacy_count"] == 0, gap

    # (c) a hash that is present but wrong is still caught
    rows = hashed_rows()
    run_sql("UPDATE audit_logs SET row_hash = 'deadbeef' WHERE id = ?", (rows["b"]["id"],))
    forged = verify_audit_log_integrity()
    assert forged["valid"] is False, forged
    assert any(item["id"] == rows["b"]["id"] for item in forged["tampered"]), forged

    # (d) rows written before hashing existed stay benign (backward compatible)
    hashed_rows()
    run_sql("DELETE FROM audit_logs")
    run_sql(
        """
        INSERT INTO audit_logs
        (id, actor, event_type, target_type, target_id, tenant_id, detail_json,
         previous_hash, row_hash, created_at)
        VALUES ('audit-legacy-1', 'legacy', 'legacy.event', 'smoke', 'legacy', 'default',
                '{}', NULL, NULL, '2020-01-01T00:00:00.000000+00:00')
        """
    )
    record_audit("audit.chain.after_legacy", "smoke", "after_legacy", {}, actor="smoke")
    with_legacy = verify_audit_log_integrity()
    assert with_legacy["valid"] is True, with_legacy
    assert with_legacy["legacy_count"] == 1, with_legacy
    assert with_legacy["degraded"] is True, with_legacy

    # --- API surface still behaves ---
    fresh_database()
    ensure_demo_users()
    first = record_audit("audit.integrity.first", "smoke", "first", {"step": 1}, actor="smoke")
    record_audit("audit.integrity.second", "smoke", "second", {"step": 2}, actor="smoke")

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"user_id": "admin", "password": "AdminPass123"})
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]

        api_initial = client.get("/api/admin/audit/integrity", headers=auth_header(token))
        assert api_initial.status_code == 200, api_initial.text
        assert api_initial.json()["valid"] is True, api_initial.json()
        assert api_initial.json()["degraded"] is False, api_initial.json()

        tamper_audit_row(first["id"])
        api_tampered = client.get("/api/admin/audit/integrity", headers=auth_header(token))
        assert api_tampered.status_code == 200, api_tampered.text
        tampered_payload = api_tampered.json()
        assert tampered_payload["valid"] is False, tampered_payload
        assert tampered_payload["tampered_count"] >= 1, tampered_payload
        assert any(item["id"] == first["id"] for item in tampered_payload["tampered"]), tampered_payload

    print("audit_integrity_smoke_test passed")


if __name__ == "__main__":
    main()
