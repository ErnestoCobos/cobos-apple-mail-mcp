"""unsubscribe_from_sender — RFC 8058 one-click unsubscribe (HTTP POST) with
a mailto: fallback. The https path uses only the stdlib (urllib), so it adds
no dependency and stays .pyz-friendly, and never touches Mail.app.

Trust boundary: the target URL comes from a sender-controlled header, so it is
treated as untrusted — only `https:` is ever requested, the request is bounded
by a hard timeout (CLAUDE.md invariant #4: never hang), and redirects to a
non-https scheme are refused.
"""

from __future__ import annotations

import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.core.errors import NotFound, ReadOnlyMode
from cobos_apple_mail_mcp.core.models import UnsubscribeResult
from cobos_apple_mail_mcp.read.emlx_parser import UnsubscribeInfo, extract_unsubscribe
from cobos_apple_mail_mcp.write.jxa_executor import JXAExecutor

_ONE_CLICK_BODY = b"List-Unsubscribe=One-Click"


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse any redirect whose target isn't https: — a sender-controlled URL
    must not be able to bounce us onto http:// (or file://, etc.)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(
                newurl, code, "refusing redirect to non-https scheme", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _post_one_click(url: str, timeout: float) -> int:
    """POST the RFC-8058 one-click body to an https URL. Raises on non-https
    or any network error; returns the HTTP status on success. Isolated so
    tests can monkeypatch it without real network access."""
    if not url.lower().startswith("https://"):
        raise ValueError(f"refusing to POST to non-https URL: {url!r}")
    req = urllib.request.Request(
        url,
        data=_ONE_CLICK_BODY,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(_ONE_CLICK_BODY)),
        },
    )
    opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        return getattr(resp, "status", None) or resp.getcode()


def _find_source(conn: sqlite3.Connection, message_id: str, account, mailbox):  # noqa: ANN001
    where = ["message_id = :mid"]
    params: dict[str, object] = {"mid": message_id}
    if account:
        where.append("(account_uuid = :account OR account_name = :account)")
        params["account"] = account
    if mailbox:
        where.append("(mailbox_name = :mailbox OR mailbox_role = :mailbox)")
        params["mailbox"] = mailbox
    return conn.execute(
        f"SELECT * FROM emails WHERE {' AND '.join(where)} ORDER BY date_received DESC",
        params,
    ).fetchone()


def unsubscribe_from_sender(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    message_id: str,
    *,
    account: str | None = None,
    mailbox: str | None = None,
    dry_run: bool = False,
) -> UnsubscribeResult:
    """Unsubscribe from the list a message belongs to. Prefers an RFC-8058
    one-click https POST; falls back to sending the mailto: unsubscribe;
    reports `none-found` (never a bare False) when the sender advertises no
    standard unsubscribe method, so the caller knows nothing happened.
    """
    # Outbound action: blocked entirely under --read-only (unlike draft ops).
    if config.server.read_only and not dry_run:
        raise ReadOnlyMode("server is running --read-only; unsubscribe is disabled")

    row = _find_source(conn, message_id, account, mailbox)
    if row is None:
        raise NotFound(f"no email found for message_id={message_id!r}")
    path = Path(row["emlx_path"]) if row["emlx_path"] else None
    if path is None or not path.exists():
        raise NotFound(f"source .emlx not on disk for message_id={message_id!r}")

    info: UnsubscribeInfo = extract_unsubscribe(path)

    # 1. RFC 8058 one-click: POST to the first https URI.
    if info.one_click and info.https_urls:
        target = info.https_urls[0]
        if dry_run:
            return UnsubscribeResult(
                method="one-click-post", ok=True, target=target, dry_run=True,
                detail="would POST 'List-Unsubscribe=One-Click' to this https URL",
            )
        try:
            status = _post_one_click(target, config.timeouts.http_sec)
        except Exception as exc:  # noqa: BLE001 - surface the failure, don't crash
            return UnsubscribeResult(
                method="one-click-post", ok=False, target=target, detail=f"POST failed: {exc}"
            )
        ok = 200 <= status < 300
        return UnsubscribeResult(
            method="one-click-post", ok=ok, target=target, detail=f"HTTP {status}"
        )

    # 2. mailto: fallback — send the unsubscribe message via the write layer.
    if info.mailto_to:
        if dry_run:
            return UnsubscribeResult(
                method="mailto", ok=True, target=info.mailto_to, dry_run=True,
                detail="would send an unsubscribe email to this address",
            )
        from cobos_apple_mail_mcp.write import compose

        send_account = row["account_name"] or row["account_uuid"]
        compose.compose_email(
            conn,
            jxa,
            config,
            account=send_account,
            to=info.mailto_to,
            subject=info.mailto_subject or "unsubscribe",
            body="",
            mode="send",
        )
        return UnsubscribeResult(
            method="mailto", ok=True, target=info.mailto_to, detail="sent unsubscribe email"
        )

    # 3. Nothing standard on offer.
    return UnsubscribeResult(
        method="none-found",
        ok=False,
        detail="the sender exposes no RFC-8058 one-click or mailto unsubscribe",
    )
