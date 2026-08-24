"""Read the inbox. Never send from here.

Two auth methods behind one interface, because which one a user needs depends
on something they cannot change:

* **Personal @gmail.com** — IMAP with an App Password. Simple, stable.
* **Google Workspace** — Google disabled App Passwords for Workspace accounts
  in 2025, so IMAP with a password is not available and OAuth via the Gmail API
  is the only route. Note that an OAuth app left in *Testing* status has its
  refresh tokens expire every seven days; publishing the app (even privately)
  is what stops a weekly re-authorisation.

``GMAIL_AUTH_METHOD`` selects between them. Everything downstream — matching,
classification, the scheduler — sees the same ``InboundEmail`` either way.

This module has no send path. Outbound lives in ``outbound.py``, is draft-only,
and requires explicit approval.
"""

from __future__ import annotations

import email as email_lib
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from backend.config import settings
from backend.integrations.matching import InboundEmail
from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["GmailReader", "ImapReader", "MailReader", "OAuthReader", "build_reader"]


_HTML_TAG = re.compile(r"<[^>]+>")


def _plain_text(raw: str) -> str:
    text = _HTML_TAG.sub(" ", raw)
    return re.sub(r"\s+", " ", text).strip()


class MailReader(Protocol):
    """What the rest of the system needs from a mailbox: recent messages."""

    def fetch_recent(self, *, since: datetime, limit: int = 200) -> list[InboundEmail]: ...


class ImapReader:
    """Personal Gmail via IMAP and an App Password."""

    def __init__(self) -> None:
        self.address = settings.gmail_address
        self.password = settings.gmail_app_password

    def _check(self) -> None:
        if not self.address or not self.password:
            raise RuntimeError(
                "GMAIL_ADDRESS and GMAIL_APP_PASSWORD are required for IMAP. "
                "A Workspace account cannot use an App Password — set "
                "GMAIL_AUTH_METHOD=oauth instead."
            )

    def fetch_recent(self, *, since: datetime, limit: int = 200) -> list[InboundEmail]:
        self._check()
        from imap_tools import AND, MailBox

        out: list[InboundEmail] = []
        with MailBox("imap.gmail.com").login(self.address, self.password) as mailbox:
            for message in mailbox.fetch(
                AND(date_gte=since.date()), limit=limit, reverse=True, mark_seen=False
            ):
                received = message.date or datetime.now(UTC)
                if received.tzinfo is None:
                    received = received.replace(tzinfo=UTC)
                out.append(
                    InboundEmail(
                        message_id=message.uid or str(message.date),
                        subject=message.subject or "",
                        from_address=message.from_ or "",
                        body=message.text or _plain_text(message.html or ""),
                        received_at=received,
                        in_reply_to=(message.headers.get("in-reply-to") or (None,))[0],
                    )
                )
        log.info("imap_fetched", count=len(out))
        return out


class OAuthReader:
    """Workspace Gmail via the Gmail API. Read-only scope, deliberately."""

    # readonly, not modify: this module has no business changing the mailbox,
    # and a narrower scope is one less thing to get wrong.
    SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)

    def _credentials(self) -> Any:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        token_file = settings.gmail_oauth_token_file
        secret_file = settings.gmail_oauth_client_secret_file
        if not secret_file:
            raise RuntimeError("GMAIL_OAUTH_CLIENT_SECRET_FILE is required for OAuth")

        credentials = None
        if token_file and token_file.exists():
            credentials = Credentials.from_authorized_user_file(str(token_file), list(self.SCOPES))

        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception as exc:  # noqa: BLE001
                # The classic symptom of an app left in Testing status.
                log.error(
                    "oauth_refresh_failed",
                    error=str(exc)[:200],
                    hint=(
                        "If your OAuth app is in Testing status, refresh tokens expire "
                        "after 7 days. Publish the app to stop re-authorising weekly."
                    ),
                )
                credentials = None

        if not credentials or not credentials.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), list(self.SCOPES))
            credentials = flow.run_local_server(port=0)
            if token_file:
                token_file.parent.mkdir(parents=True, exist_ok=True)
                token_file.write_text(credentials.to_json(), encoding="utf-8")

        return credentials

    def fetch_recent(self, *, since: datetime, limit: int = 200) -> list[InboundEmail]:
        from googleapiclient.discovery import build

        service = build("gmail", "v1", credentials=self._credentials(), cache_discovery=False)
        query = f"after:{int(since.timestamp())}"

        listing = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=limit)
            .execute()
        )

        out: list[InboundEmail] = []
        for stub in listing.get("messages", []):
            detail = (
                service.users()
                .messages()
                .get(userId="me", id=stub["id"], format="full")
                .execute()
            )
            out.append(self._to_email(detail))
        log.info("gmail_api_fetched", count=len(out))
        return out

    @staticmethod
    def _to_email(payload: dict[str, Any]) -> InboundEmail:
        headers = {
            header["name"].lower(): header["value"]
            for header in payload.get("payload", {}).get("headers", [])
        }
        received = datetime.fromtimestamp(int(payload.get("internalDate", 0)) / 1000, tz=UTC)

        body = ""
        for part in OAuthReader._walk(payload.get("payload", {})):
            if part.get("mimeType") == "text/plain":
                body = OAuthReader._decode(part)
                break
        if not body:
            for part in OAuthReader._walk(payload.get("payload", {})):
                if part.get("mimeType") == "text/html":
                    body = _plain_text(OAuthReader._decode(part))
                    break

        return InboundEmail(
            message_id=payload.get("id", ""),
            subject=headers.get("subject", ""),
            from_address=headers.get("from", ""),
            body=body,
            received_at=received,
            thread_id=payload.get("threadId"),
            in_reply_to=headers.get("in-reply-to"),
        )

    @staticmethod
    def _walk(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
        yield part
        for child in part.get("parts", []) or []:
            yield from OAuthReader._walk(child)

    @staticmethod
    def _decode(part: dict[str, Any]) -> str:
        import base64

        data = part.get("body", {}).get("data")
        if not data:
            return ""
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - a bad part is not a failed fetch
            return ""


def build_reader() -> MailReader:
    """The reader the configuration asks for."""
    if settings.gmail_auth_method == "oauth":
        return OAuthReader()
    return ImapReader()


# Kept as an alias so callers read naturally.
GmailReader = build_reader


def parse_rfc822(raw: bytes) -> InboundEmail:
    """Parse a raw message. Used by tests and by any future import path."""
    message = email_lib.message_from_bytes(raw)
    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                break
    else:
        payload = message.get_payload(decode=True)
        body = payload.decode("utf-8", errors="replace") if payload else ""

    received = datetime.now(UTC)
    if message.get("date"):
        parsed = email_lib.utils.parsedate_to_datetime(message["date"])
        if parsed:
            received = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    return InboundEmail(
        message_id=message.get("message-id", ""),
        subject=message.get("subject", ""),
        from_address=message.get("from", ""),
        body=body,
        received_at=received,
        in_reply_to=message.get("in-reply-to"),
    )


def default_since() -> datetime:
    """How far back a sweep looks by default."""
    return datetime.now(UTC) - timedelta(days=7)
