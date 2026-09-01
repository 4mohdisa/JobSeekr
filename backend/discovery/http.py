"""One httpx client factory for every discovery source.

Discovery is HTTP only (Claude.md) — no browser, no authenticated session. That
makes the client boring, and boring is the point: identical headers, timeouts
and retry policy everywhere, configured once, so a new source cannot
accidentally ship without a timeout or with a default python-httpx user agent.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["DEFAULT_HEADERS", "build_client", "get_with_retry"]


# A real desktop Chrome fingerprint. Job boards serve different (or no) markup
# to obvious scripts, so this is about getting the same page a browser gets,
# not about hiding.
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
    # Only what httpx can actually decode. A real Chrome also advertises "br",
    # but brotli support in httpx needs an optional dependency that is not
    # installed here, so advertising it invites a body this client cannot read.
    # Seek happens to answer gzip, which is why it never bit; a server that
    # honoured "br" would have failed with an unreadable response.
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

# Retried: the transport-level failures that genuinely resolve on a retry.
# Not retried: 403/404, which mean the endpoint changed or is blocked and
# hammering it makes things worse.
_RETRYABLE = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


def build_client(
    *,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
    **kwargs: Any,
) -> httpx.Client:
    """A configured client. Callers own closing it — use it as a context manager."""
    merged = dict(DEFAULT_HEADERS)
    if headers:
        merged.update(headers)
    return httpx.Client(
        headers=merged,
        timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT),
        follow_redirects=follow_redirects,
        **kwargs,
    )


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def get_with_retry(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    """GET with backoff on transport failures only.

    HTTP error statuses are returned to the caller rather than raised: a source
    decides for itself whether a 403 means "fall back to HTML" or "give up".
    """
    response = client.get(url, **kwargs)
    log.debug("http_get", url=str(response.url)[:200], status=response.status_code)
    return response
