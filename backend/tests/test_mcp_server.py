"""Tests for the MCP read-only server gate and tool registration.

Business invariants:
- The endpoint must be invisible (404) when disabled or unconfigured.
- A wrong or missing token must never reach the tools (401).
- Exactly the five read-only tools are exposed; nothing that writes.
"""

import asyncio

from app.mcp_server import evaluate_mcp_access, mcp


def test_disabled_returns_404():
    assert evaluate_mcp_access(enabled=False, configured_token="tok", authorization="Bearer tok") == (404, "Not Found")


def test_enabled_without_token_returns_404():
    assert evaluate_mcp_access(enabled=True, configured_token=None, authorization=None) == (404, "Not Found")


def test_missing_bearer_returns_401():
    assert evaluate_mcp_access(enabled=True, configured_token="tok", authorization=None)[0] == 401
    assert evaluate_mcp_access(enabled=True, configured_token="tok", authorization="Basic xyz")[0] == 401


def test_wrong_token_returns_401():
    assert evaluate_mcp_access(enabled=True, configured_token="tok", authorization="Bearer wrong")[0] == 401


def test_correct_token_passes():
    assert evaluate_mcp_access(enabled=True, configured_token="tok", authorization="Bearer tok") is None


def test_exactly_five_readonly_tools():
    tools = asyncio.run(mcp.get_tools())
    names = {name for name in tools}
    assert names == {
        "list_invoices",
        "get_invoice_detail",
        "get_statistics",
        "list_attachments",
        "get_invoice_file",
    }
