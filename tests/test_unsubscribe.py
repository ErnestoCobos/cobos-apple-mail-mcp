from __future__ import annotations

import pytest

from cobos_apple_mail_mcp.config import load_config
from cobos_apple_mail_mcp.core.errors import NotFound, ReadOnlyMode
from cobos_apple_mail_mcp.read.emlx_parser import extract_unsubscribe
from cobos_apple_mail_mcp.read.indexer import build_index
from cobos_apple_mail_mcp.storage.database import connect_index
from cobos_apple_mail_mcp.write import unsubscribe
from tests.helpers import FakeJXAExecutor, write_message


def _cfg(**overrides):
    return load_config(cli_overrides=overrides, environ={})


# ---- header parsing (extract_unsubscribe) --------------------------------


def test_parse_one_click_https_and_mailto(tmp_path):
    path = write_message(
        tmp_path,
        rowid=1,
        message_id="m1@x.com",
        list_unsubscribe="<mailto:unsub@list.example.com?subject=stop>, "
        "<https://list.example.com/u/abc>",
        list_unsubscribe_post="List-Unsubscribe=One-Click",
    )
    info = extract_unsubscribe(path)
    assert info.one_click is True
    assert info.https_urls == ["https://list.example.com/u/abc"]
    assert info.mailto_to == "unsub@list.example.com"
    assert info.mailto_subject == "stop"


def test_parse_mailto_only(tmp_path):
    path = write_message(
        tmp_path,
        rowid=1,
        message_id="m1@x.com",
        list_unsubscribe="<mailto:leave@list.example.com>",
    )
    info = extract_unsubscribe(path)
    assert info.one_click is False
    assert info.https_urls == []
    assert info.mailto_to == "leave@list.example.com"


def test_parse_none(tmp_path):
    path = write_message(tmp_path, rowid=1, message_id="m1@x.com")
    info = extract_unsubscribe(path)
    assert info.https_urls == []
    assert info.mailto_to is None
    assert info.one_click is False


def test_parse_http_url_is_not_treated_as_https(tmp_path):
    # A non-https URL must never land in https_urls (it would otherwise be a
    # POST target).
    path = write_message(
        tmp_path,
        rowid=1,
        message_id="m1@x.com",
        list_unsubscribe="<http://insecure.example.com/u/abc>",
        list_unsubscribe_post="List-Unsubscribe=One-Click",
    )
    info = extract_unsubscribe(path)
    assert info.https_urls == []
    assert info.mailto_to is None


# ---- the tool (unsubscribe_from_sender) ----------------------------------


def _seed(tmp_path, **msg_kwargs):
    write_message(tmp_path, rowid=1, message_id="m1@x.com", mailbox="INBOX", **msg_kwargs)
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    return conn


def test_one_click_posts_and_reports(tmp_path, monkeypatch):
    conn = _seed(
        tmp_path,
        list_unsubscribe="<https://list.example.com/u/abc>",
        list_unsubscribe_post="List-Unsubscribe=One-Click",
    )
    posted = {}

    def fake_post(url, timeout):
        posted["url"] = url
        posted["timeout"] = timeout
        return 200

    monkeypatch.setattr(unsubscribe, "_post_one_click", fake_post)
    result = unsubscribe.unsubscribe_from_sender(conn, FakeJXAExecutor(), _cfg(), "m1@x.com")
    assert result.method == "one-click-post"
    assert result.ok is True
    assert posted["url"] == "https://list.example.com/u/abc"


def test_one_click_dry_run_makes_no_request(tmp_path, monkeypatch):
    conn = _seed(
        tmp_path,
        list_unsubscribe="<https://list.example.com/u/abc>",
        list_unsubscribe_post="List-Unsubscribe=One-Click",
    )

    def boom(url, timeout):
        raise AssertionError("dry_run must not POST")

    monkeypatch.setattr(unsubscribe, "_post_one_click", boom)
    result = unsubscribe.unsubscribe_from_sender(
        conn, FakeJXAExecutor(), _cfg(), "m1@x.com", dry_run=True
    )
    assert result.dry_run is True
    assert result.method == "one-click-post"


def test_mailto_fallback_sends_via_compose(tmp_path):
    conn = _seed(tmp_path, list_unsubscribe="<mailto:leave@list.example.com>")
    jxa = FakeJXAExecutor()
    jxa.on("ping", lambda args: {"ok": True})
    # compose_email builds an OutgoingMessage; stub the JXA it will invoke.
    jxa.on("composeEmail", lambda args: {"status": "sent"})
    result = unsubscribe.unsubscribe_from_sender(conn, jxa, _cfg(), "m1@x.com")
    assert result.method == "mailto"
    assert result.target == "leave@list.example.com"
    assert any(name == "composeEmail" for name, _ in jxa.calls)


def test_none_found_reports_honestly(tmp_path):
    conn = _seed(tmp_path)  # no List-Unsubscribe
    result = unsubscribe.unsubscribe_from_sender(conn, FakeJXAExecutor(), _cfg(), "m1@x.com")
    assert result.method == "none-found"
    assert result.ok is False


def test_read_only_blocks_unsubscribe(tmp_path):
    conn = _seed(
        tmp_path,
        list_unsubscribe="<https://list.example.com/u/abc>",
        list_unsubscribe_post="List-Unsubscribe=One-Click",
    )
    with pytest.raises(ReadOnlyMode):
        unsubscribe.unsubscribe_from_sender(
            conn, FakeJXAExecutor(), _cfg(server={"read_only": True}), "m1@x.com"
        )


def test_unknown_message_raises_not_found(tmp_path):
    conn = _seed(tmp_path)
    with pytest.raises(NotFound):
        unsubscribe.unsubscribe_from_sender(conn, FakeJXAExecutor(), _cfg(), "nope@x.com")


def test_post_one_click_rejects_non_https():
    with pytest.raises(ValueError, match="non-https"):
        unsubscribe._post_one_click("http://insecure.example.com/u", 5.0)
