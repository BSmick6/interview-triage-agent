"""MCP server exposing the triage API as agent tools.

Run standalone with:  uv run python -m triage.mcp_server
The agent spawns this over stdio.
"""

from __future__ import annotations

import json
import os

import httpx
from mcp.server import MCPServer

API_BASE = os.environ.get("TRIAGE_API", "http://127.0.0.1:8787")

server = MCPServer(name="triage-tools")


async def _get(path: str, params: dict | None = None) -> str:
    async with httpx.AsyncClient(base_url=API_BASE, timeout=15) as client:
        r = await client.get(path, params=params or {})
        return json.dumps(r.json(), indent=2)


@server.tool()
async def get_case(case_id: str) -> str:
    """Fetch a flagged case: the transaction, why the rules engine flagged it,
    and its current status. Example case_id: case_501."""
    return await _get(f"/cases/{case_id}")


@server.tool()
async def get_account(account_id: str) -> str:
    """Fetch the account holder profile: when the account was opened, its
    status, and its risk tier. Example account_id: acct_11."""
    return await _get(f"/accounts/{account_id}")


@server.tool()
async def list_transactions(account_id: str, days: int = 30, limit: int = 50) -> str:
    """List recent transactions for an account, newest first.

    Use `days` to bound the lookback window and `limit` to bound the row
    count. A 30-day window is enough to establish a spending baseline.
    """
    return await _get(
        f"/accounts/{account_id}/transactions",
        {"days": 3650, "limit": 500},
    )


@server.tool()
async def get_merchant(merchant_id: str) -> str:
    """Fetch a merchant's category and any standing risk notes.
    Example merchant_id: mrc_delta."""
    return await _get(f"/merchants/{merchant_id}")


@server.tool()
async def record_decision(case_id: str, decision: str, rationale: str) -> str:
    """Record the final triage decision for a case. Call this exactly once,
    after gathering evidence.

    decision must be one of: approve, block, needs_review.
    rationale must cite the specific transaction ids / merchant ids that
    drove the decision.
    """
    payload = {"case_id": case_id, "decision": decision, "rationale": rationale}
    last_error = "unknown error"
    async with httpx.AsyncClient(base_url=API_BASE, timeout=15) as client:
        for _ in range(3):
            try:
                r = await client.post("/decisions", json=payload)
                if r.status_code < 400:
                    return json.dumps(r.json(), indent=2)
                last_error = f"HTTP {r.status_code}: {r.text}"
                if r.status_code < 500:
                    break  # 4xx won't change on retry; hand it back to the model
            except httpx.HTTPError as exc:
                last_error = str(exc)
    return json.dumps({"error": last_error}, indent=2)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
