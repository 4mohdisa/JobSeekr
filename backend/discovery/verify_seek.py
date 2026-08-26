"""Confirm, from the user's own machine, what Seek's search endpoint really is.

WHY THIS EXISTS
---------------
The build spec says: find the real endpoint by inspecting network traffic from
a seek.com.au search — do not guess it. That could not be done where this code
was written: ``www.seek.com.au`` is blocked there by network policy, so no
request could be issued and no traffic inspected.

This command is the honest substitute. It runs where Seek *is* reachable, tries
each candidate in turn, reports exactly what came back, and prints the ``.env``
lines that make :mod:`backend.discovery.seek_source` use whatever actually
worked. It never writes ``.env`` itself — the user stays in the loop.

    uv run python -m backend.discovery.verify_seek \\
        --terms "python developer" --where "Adelaide SA"

Read the output, paste the suggested lines into ``.env``, done.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from backend.config import settings
from backend.discovery.http import build_client
from backend.discovery.seek_source import (
    parse_json_payload,
    parse_jsonld,
    parse_page_state,
)
from backend.logging_setup import configure_logging, get_logger

log = get_logger(__name__)


def _emit(text: str = "") -> None:
    """Write to stdout.

    This is a human-facing CLI report, not application logging, so it goes to
    the stream the user is reading. structlog still carries the machine record.
    """
    sys.stdout.write(text + "\n")


def _probe_json(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"url": url, "ok": False}
    try:
        response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["status"] = response.status_code
    result["content_type"] = response.headers.get("content-type", "")
    if response.status_code != 200:
        result["body_head"] = response.text[:300]
        return result

    try:
        payload = response.json()
    except ValueError:
        result["error"] = "200 but body is not JSON"
        result["body_head"] = response.text[:300]
        return result

    if not isinstance(payload, dict):
        result["error"] = f"JSON root is {type(payload).__name__}, expected object"
        return result

    jobs = parse_json_payload(payload)
    result["ok"] = bool(jobs)
    result["top_level_keys"] = sorted(payload)[:25]
    result["parsed_jobs"] = len(jobs)
    if jobs:
        result["sample"] = json.loads(jobs[0].model_dump_json())
    return result


def _probe_html(client: httpx.Client, params: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"url": settings.seek_html_search_url, "ok": False}
    try:
        response = client.get(
            settings.seek_html_search_url,
            params=params,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
    except httpx.HTTPError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["status"] = response.status_code
    if response.status_code != 200:
        return result

    state_jobs = parse_page_state(response.text)
    jsonld_jobs = parse_jsonld(response.text)
    result["page_state_jobs"] = len(state_jobs)
    result["jsonld_jobs"] = len(jsonld_jobs)
    result["ok"] = bool(state_jobs or jsonld_jobs)
    winner = state_jobs or jsonld_jobs
    if winner:
        result["strategy"] = "page_state" if state_jobs else "jsonld"
        result["sample"] = json.loads(winner[0].model_dump_json())
    return result


def _report(name: str, result: dict[str, Any]) -> None:
    mark = "WORKS" if result.get("ok") else "no"
    _emit(f"[{mark:>5}] {name}")
    _emit(f"         url: {result.get('url')}")
    for key in (
        "status",
        "content_type",
        "error",
        "parsed_jobs",
        "page_state_jobs",
        "jsonld_jobs",
        "strategy",
    ):
        if key in result:
            _emit(f"         {key}: {result[key]}")
    if result.get("top_level_keys"):
        _emit(f"         top-level JSON keys: {result['top_level_keys']}")
    if result.get("body_head"):
        _emit(f"         body head: {result['body_head'][:200]!r}")
    if result.get("sample"):
        _emit("         sample record:")
        for line in json.dumps(result["sample"], indent=2)[:1600].splitlines():
            _emit(f"           {line}")
    _emit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.discovery.verify_seek",
        description=(
            "Probe Seek's search endpoints from this machine and report which "
            "one actually works, then print the .env lines to make the "
            "discovery adapter use it."
        ),
    )
    parser.add_argument("--terms", default="python developer", help="keywords to search")
    parser.add_argument("--where", default="Adelaide SA", help="location to search")
    parser.add_argument("--page-size", type=int, default=settings.seek_page_size)
    args = parser.parse_args(argv)

    configure_logging()

    params = {
        "siteKey": settings.seek_site_key,
        "sourcesystem": settings.seek_source_system,
        "keywords": args.terms,
        "where": args.where,
        "page": 1,
        "pageSize": args.page_size,
        "locale": settings.seek_locale,
    }

    _emit("Probing Seek search endpoints")
    _emit(f"  keywords={args.terms!r} where={args.where!r}")
    _emit("=" * 72)
    _emit()

    results: list[tuple[str, dict[str, Any]]] = []
    with build_client() as client:
        results.append(
            ("JSON endpoint (configured)", _probe_json(client, settings.seek_search_url, params))
        )
        results.append(
            (
                "JSON endpoint (fallback)",
                _probe_json(client, settings.seek_search_url_fallback, params),
            )
        )
        results.append(
            (
                "HTML search page (page state / JSON-LD)",
                _probe_html(client, {"keywords": args.terms, "where": args.where}),
            )
        )

    for name, result in results:
        _report(name, result)

    _emit("=" * 72)
    winners = [(name, r) for name, r in results if r.get("ok")]
    if not winners:
        _emit("NOTHING WORKED.")
        _emit()
        _emit("That means one of:")
        _emit("  * this machine cannot reach seek.com.au (proxy, firewall, VPN)")
        _emit("  * Seek changed its contract and the parsers need updating")
        _emit("Open a seek.com.au search in a browser, open DevTools > Network,")
        _emit("filter to XHR, and look for the search request. Then set")
        _emit("SEEK_SEARCH_URL in .env to its path and re-run this command.")
        return 1

    name, best = winners[0]
    _emit(f"WORKING: {name}")
    _emit()
    _emit("Paste into .env:")
    _emit()
    if best["url"] != settings.seek_html_search_url:
        _emit(f"SEEK_SEARCH_URL={best['url']}")
    else:
        _emit("# The JSON API did not respond; the HTML fallback carried the results.")
        _emit("# Discovery still works, but confirm the JSON endpoint when you can.")
    _emit(f"SEEK_SITE_KEY={settings.seek_site_key}")
    _emit(f"SEEK_SOURCE_SYSTEM={settings.seek_source_system}")
    _emit(f"SEEK_LOCALE={settings.seek_locale}")
    _emit(f"SEEK_PAGE_SIZE={args.page_size}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
