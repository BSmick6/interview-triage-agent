"""Core banking / case-management API.

This is the system of record. The MCP server is a client of this API; so are
the ops console and the batch reconciliation jobs (not in this repo).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field

from . import db

app = FastAPI(title="Triage API", version="0.1.0")

# Accounts pinned to the degraded write shard. See INFRA-2291.
DEGRADED_SHARD = {"case_504"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


class DecisionIn(BaseModel):
    case_id: str = Field(..., description="Case being decided")
    decision: str = Field(..., description="approve | block | needs_review")
    rationale: str = Field(..., description="Evidence-backed explanation")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/cases/{case_id}")
def get_case(case_id: str):
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT c.*, t.amount_cents, t.ts AS txn_ts, t.merchant_id, t.description
               FROM cases c JOIN transactions t ON t.id = c.txn_id
               WHERE c.id = ?""",
            (case_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"case {case_id} not found")
        return dict(row)
    finally:
        conn.close()


@app.get("/accounts/{account_id}")
def get_account(account_id: str):
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"account {account_id} not found")
        return dict(row)
    finally:
        conn.close()


@app.get("/accounts/{account_id}/transactions")
def list_transactions(
    account_id: str,
    days: int = Query(30, ge=1, le=3650),
    limit: int = Query(50, ge=1, le=500),
):
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT id, ts, amount_cents, merchant_id, description
               FROM transactions
               WHERE account_id = ?
                 AND ts >= datetime('now', ?)
               ORDER BY ts DESC
               LIMIT ?""",
            (account_id, f"-{days} days", limit),
        ).fetchall()
        return {"account_id": account_id, "days": days, "count": len(rows),
                "transactions": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/accounts/{account_id}/prior-cases")
def list_prior_cases(account_id: str):
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT id, txn_id, status, flagged_reason, outcome, created_at
               FROM cases WHERE account_id = ? AND status = 'closed'
               ORDER BY created_at DESC""",
            (account_id,),
        ).fetchall()
        return {"account_id": account_id, "prior_cases": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/merchants/{merchant_id}")
def get_merchant(merchant_id: str):
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM merchants WHERE id = ?", (merchant_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"merchant {merchant_id} not found")
        return dict(row)
    finally:
        conn.close()


@app.post("/decisions", status_code=201)
def record_decision(body: DecisionIn, response: Response):
    """Record the one decision a case gets.

    The decision row, the case close and the audit entry commit in a single
    transaction, so a client that times out after the commit and retries finds
    a consistent ledger. A retry carrying the same decision is replayed
    (200, no new row); a different decision on an already-decided case is a
    409 -- nothing overwrites an outcome already on file, human or agent.
    """
    conn = db.connect()
    try:
        # Take the write lock up front so two concurrent posts for the same
        # case cannot both pass the "already decided?" check.
        conn.execute("BEGIN IMMEDIATE")
        case = conn.execute("SELECT id FROM cases WHERE id = ?", (body.case_id,)).fetchone()
        if case is None:
            raise HTTPException(404, f"case {body.case_id} not found")

        existing = conn.execute(
            "SELECT id, decision FROM decisions WHERE case_id = ? ORDER BY id LIMIT 1",
            (body.case_id,),
        ).fetchone()
        if existing is not None:
            if existing["decision"] != body.decision:
                raise HTTPException(
                    409,
                    f"case {body.case_id} already decided as '{existing['decision']}'; "
                    "decisions are final and cannot be overwritten",
                )
            response.status_code = 200
            return {"decision_id": existing["id"], "case_id": body.case_id,
                    "decision": existing["decision"], "replayed": True}

        now = _now()
        cur = conn.execute(
            "INSERT INTO decisions (case_id, decision, rationale, created_at) VALUES (?,?,?,?)",
            (body.case_id, body.decision, body.rationale, now),
        )
        conn.execute("UPDATE cases SET status = 'closed', outcome = ? WHERE id = ?",
                     (body.decision, body.case_id))
        _insert_audit(conn, ts=now, actor="agent:triage", action="decision.recorded",
                      case_id=body.case_id, detail=body.decision)
        conn.commit()
        decision_id = cur.lastrowid
    finally:
        conn.rollback()  # no-op after commit; releases the lock on early exits
        conn.close()

    # Simulated upstream timeout: the write above is already durable, the
    # client just never hears back. Retries must be safe (see replay above).
    if body.case_id in DEGRADED_SHARD:
        raise HTTPException(503, "upstream timeout")

    return {"decision_id": decision_id, "case_id": body.case_id, "decision": body.decision}


def _insert_audit(conn, *, ts: str, actor: str, action: str, case_id: str | None,
                  detail: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, case_id, detail) VALUES (?,?,?,?,?)",
        (ts, actor, action, case_id, detail),
    )


def write_audit(*, actor: str, action: str, case_id: str | None, detail: str) -> None:
    conn = db.connect()
    try:
        _insert_audit(conn, ts=_now(), actor=actor, action=action, case_id=case_id,
                      detail=detail)
        conn.commit()
    finally:
        conn.close()


@app.get("/decisions/{case_id}")
def get_decisions(case_id: str):
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE case_id = ? ORDER BY id", (case_id,)
        ).fetchall()
        return {"case_id": case_id, "decisions": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/audit")
def get_audit(case_id: str | None = None):
    conn = db.connect()
    try:
        if case_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE case_id = ? ORDER BY id", (case_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return {"entries": [dict(r) for r in rows]}
    finally:
        conn.close()
