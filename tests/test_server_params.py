"""Regression tests for tolerant tool-parameter coercion (server.StrList).

Observed live: Cowork serialized list parameters as a JSON *string*
('["/path/a.pdf"]'), so strict list[str] validation rejected manage_drafts /
compose_email attachments with "Input should be a valid list". The StrList
alias must accept a real list, a JSON-stringified list, or a bare string —
while keeping the advertised JSON schema a plain array of strings.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from cobos_apple_mail_mcp.server import StrList, _coerce_str_list

_ADAPTER = TypeAdapter(StrList)


def test_real_list_passes_through():
    assert _ADAPTER.validate_python(["a.pdf", "b.pdf"]) == ["a.pdf", "b.pdf"]


def test_json_stringified_list_is_parsed():
    value = '["/Users/x/Claude/Propuesta_Cobos_E-Solutions.pdf", "/tmp/b.pdf"]'
    assert _ADAPTER.validate_python(value) == [
        "/Users/x/Claude/Propuesta_Cobos_E-Solutions.pdf",
        "/tmp/b.pdf",
    ]


def test_bare_string_becomes_single_item_list():
    assert _ADAPTER.validate_python("m1@example.com") == ["m1@example.com"]


def test_json_list_with_non_string_items_is_stringified():
    assert _ADAPTER.validate_python("[1, 2]") == ["1", "2"]


def test_malformed_bracket_string_degrades_to_single_item():
    # Unparseable-as-JSON but bracket-leading: treated as one opaque item
    # (fails later with a clear not-found error) rather than a validation crash.
    assert _ADAPTER.validate_python("[not json") == ["[not json"]


def test_bracket_string_with_trailing_junk_degrades_to_single_item():
    assert _ADAPTER.validate_python('["ok"] extra') == ['["ok"] extra']


def test_non_string_non_list_still_rejected():
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(42)


def test_schema_still_advertises_plain_array():
    assert _ADAPTER.json_schema() == {"type": "array", "items": {"type": "string"}}


def test_coerce_helper_passes_none_through():
    assert _coerce_str_list(None) is None


def test_fastmcp_call_path_applies_coercion(monkeypatch):
    """End-to-end through FastMCP's real validation pipeline: build the actual
    server, call move_email over an in-memory client with message_ids as a
    JSON-stringified list, and assert the write layer receives a real list.
    Proves FastMCP preserves the Annotated BeforeValidator (if it stripped
    Annotated metadata, the client-side string would still be rejected)."""
    import asyncio

    from fastmcp import Client

    from cobos_apple_mail_mcp.config import load_config
    from cobos_apple_mail_mcp.server import build_server

    captured: dict = {}

    def fake_move_email(conn, jxa, config, message_ids, to_mailbox, **kwargs):
        captured["message_ids"] = message_ids
        captured["to_mailbox"] = to_mailbox
        captured["to_account"] = kwargs.get("to_account")
        return {"ok": True}

    monkeypatch.setattr(
        "cobos_apple_mail_mcp.tools.write_tools.move_email", fake_move_email
    )

    cfg = load_config(cli_overrides={"index": {"path": ":memory:"}}, environ={})
    mcp = build_server(cfg)

    async def run() -> None:
        async with Client(mcp) as client:
            await client.call_tool(
                "move_email",
                {
                    "message_ids": '["m1@x.com", "m2@x.com"]',
                    "to_mailbox": "Archive",
                    "to_account": "Work",
                },
            )

    asyncio.run(run())

    assert captured["message_ids"] == ["m1@x.com", "m2@x.com"]
    assert isinstance(captured["message_ids"], list)
    assert captured["to_mailbox"] == "Archive"
    assert captured["to_account"] == "Work"
