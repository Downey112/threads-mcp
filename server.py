# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.27"]
# ///
"""Read-only Threads (Meta) MCP server for topic analysis.

Run as an MCP server:   uv run --script server.py
One-time token setup:   uv run --script server.py setup
Check the token:        uv run --script server.py status
"""
from __future__ import annotations

import csv
import getpass
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import logging

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# httpx logs full request URLs, which carry the access token.
logging.getLogger("httpx").setLevel(logging.WARNING)

API_BASE = os.environ.get("THREADS_API_BASE", "https://graph.threads.com/v1.0")
HOME = Path(os.environ.get("THREADS_MCP_HOME", Path.home() / ".threads-mcp"))
TOKEN_FILE = HOME / "token.json"
EXPORT_DIR = Path(os.environ.get("THREADS_EXPORT_DIR", Path(__file__).parent / "exports"))

POST_FIELDS = "id,text,media_type,permalink,timestamp,username,has_replies,is_quote_post,is_reply"
REPLY_FIELDS = "id,text,username,permalink,timestamp,media_type,has_replies,is_reply,replied_to,root_post"
PROFILE_FIELDS = (
    "username,name,profile_picture_url,biography,is_verified,follower_count,"
    "likes_count,quotes_count,replies_count,reposts_count,views_count"
)
MEDIA_METRICS = "views,likes,replies,reposts,quotes,shares"
USER_METRICS = "views,likes,replies,reposts,quotes,followers_count"

REFRESH_AFTER_SECONDS = 3 * 24 * 3600  # refresh well inside the 60-day lifetime


class ThreadsError(ToolError):
    """Expected API/token failure; its message reaches the model as-is."""


# ---------------------------------------------------------------- token store

def _load_token() -> dict[str, Any]:
    if not TOKEN_FILE.exists():
        raise ThreadsError(
            f"No Threads token at {TOKEN_FILE}. Run: uv run --script server.py setup"
        )
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))


def _save_token(data: dict[str, Any]) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _raw_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = path if path.startswith("http") else f"{API_BASE}/{path.lstrip('/')}"
    resp = httpx.get(url, params=params, timeout=30)
    try:
        body = resp.json()
    except ValueError:
        raise ThreadsError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    if resp.status_code >= 400 or "error" in body:
        err = body.get("error", body)
        raise ThreadsError(
            f"HTTP {resp.status_code}: {err.get('message', err)} "
            f"(type={err.get('type')}, code={err.get('code')})"
        )
    return body


def _access_token() -> str:
    tok = _load_token()
    age = time.time() - tok.get("obtained_at", 0)
    since_attempt = time.time() - tok.get("last_refresh_attempt", 0)
    if age > REFRESH_AFTER_SECONDS and since_attempt > 24 * 3600:
        tok["last_refresh_attempt"] = time.time()
        try:
            body = _raw_get(
                "refresh_access_token",
                {"grant_type": "th_refresh_token", "access_token": tok["access_token"]},
            )
            tok = {
                **tok,
                "last_refresh_error": None,
                "access_token": body["access_token"],
                "obtained_at": time.time(),
                "expires_at": time.time() + int(body.get("expires_in", 0)) if body.get("expires_in") else None,
            }
            _save_token(tok)
        except ThreadsError as e:
            # Not fatal while the old token is still valid; surface it in status.
            tok["last_refresh_error"] = str(e)
            _save_token(tok)
    return tok["access_token"]


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    params["access_token"] = _access_token()
    return _raw_get(path, params)


def _paged(path: str, params: dict[str, Any], pages: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    body = _get(path, params)
    rows.extend(body.get("data", []))
    for _ in range(max(pages, 1) - 1):
        nxt = body.get("paging", {}).get("next")
        if not nxt:
            break
        # The next URL already carries every param, including the token.
        body = _raw_get(nxt, {})
        rows.extend(body.get("data", []))
    return rows


def _export(rows: list[dict[str, Any]], name: str | None) -> str | None:
    if not name or not rows:
        return None
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    path = EXPORT_DIR / f"{safe}.csv"
    cols = sorted({k for r in rows for k in r})
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in r.items()})
    return str(path)


def _result(rows: list[dict[str, Any]], export_name: str | None, **meta: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"count": len(rows), **meta, "data": rows}
    saved = _export(rows, export_name)
    if saved:
        out["csv"] = saved
    return out


# ---------------------------------------------------------------- MCP tools

mcp = MCPServer("threads")


@mcp.tool()
def threads_status() -> dict[str, Any]:
    """Check the Threads connection: who the token belongs to and when it expires."""
    me = _get("me", {"fields": "id,username,name,threads_profile_picture_url"})
    tok = _load_token()
    exp = tok.get("expires_at")
    return {
        "user": me,
        "token_obtained": datetime.fromtimestamp(tok["obtained_at"], timezone.utc).isoformat(),
        "token_expires": datetime.fromtimestamp(exp, timezone.utc).isoformat() if exp else "unknown (refreshes automatically)",
        "last_refresh_error": tok.get("last_refresh_error"),
        "api_base": API_BASE,
    }


@mcp.tool()
def threads_keyword_search(
    q: str,
    search_type: Literal["TOP", "RECENT"] = "TOP",
    search_mode: Literal["KEYWORD", "TAG"] = "KEYWORD",
    media_type: Literal["TEXT", "IMAGE", "VIDEO"] | None = None,
    since: str | None = None,
    until: str | None = None,
    author_username: str | None = None,
    limit: int = 100,
    pages: int = 1,
    export_name: str | None = None,
) -> dict[str, Any]:
    """Search public Threads posts by keyword or topic tag (search_mode=TAG).

    since/until accept a Unix timestamp or a date like 2026-10-01. limit is per
    page (max 100); pages follows pagination. Each call counts against the
    2,200 queries / 24h quota. Until Meta approves threads_keyword_search for
    the app, results only include the token owner's own posts.
    export_name saves the rows as exports/<export_name>.csv.
    """
    params = {
        "q": q,
        "search_type": search_type,
        "search_mode": search_mode,
        "media_type": media_type,
        "since": since,
        "until": until,
        "author_username": author_username.lstrip("@") if author_username else None,
        "limit": min(max(limit, 1), 100),
        "fields": POST_FIELDS,
    }
    rows = _paged("keyword_search", params, pages)
    return _result(rows, export_name, query=q, search_type=search_type, search_mode=search_mode)


@mcp.tool()
def threads_profile_lookup(username: str) -> dict[str, Any]:
    """Look up a public Threads profile: bio, followers, and 7-day likes/replies/reposts/views.

    Needs threads_profile_discovery approval for accounts other than Meta's own
    (@meta, @threads, @instagram, @facebook). Profiles need 100+ followers.
    """
    return _get("profile_lookup", {"username": username.lstrip("@"), "fields": PROFILE_FIELDS})


@mcp.tool()
def threads_profile_posts(
    username: str,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    pages: int = 1,
    export_name: str | None = None,
) -> dict[str, Any]:
    """List a public profile's posts, newest first. Same access rules as threads_profile_lookup."""
    params = {
        "username": username.lstrip("@"),
        "since": since,
        "until": until,
        "limit": min(max(limit, 1), 100),
        "fields": POST_FIELDS,
    }
    rows = _paged("profile_posts", params, pages)
    return _result(rows, export_name, username=username.lstrip("@"))


@mcp.tool()
def threads_post_replies(
    media_id: str,
    scope: Literal["replies", "conversation"] = "replies",
    reverse: bool = False,
    pages: int = 1,
    export_name: str | None = None,
) -> dict[str, Any]:
    """Get replies to a post. scope=replies gives top-level replies; conversation gives
    every reply at any depth. Meta may refuse this for posts you don't own."""
    rows = _paged(media_id + "/" + scope, {"fields": REPLY_FIELDS, "reverse": str(reverse).lower()}, pages)
    return _result(rows, export_name, media_id=media_id, scope=scope)


@mcp.tool()
def threads_get_post(media_id: str) -> dict[str, Any]:
    """Fetch one post by its media ID."""
    return _get(media_id, {"fields": POST_FIELDS})


@mcp.tool()
def threads_my_posts(
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    pages: int = 1,
    export_name: str | None = None,
) -> dict[str, Any]:
    """List the token owner's own posts."""
    params = {"since": since, "until": until, "limit": min(max(limit, 1), 100), "fields": POST_FIELDS}
    rows = _paged("me/threads", params, pages)
    return _result(rows, export_name)


@mcp.tool()
def threads_post_insights(media_id: str) -> dict[str, Any]:
    """Views, likes, replies, reposts, quotes and shares for one of your own posts."""
    body = _get(media_id + "/insights", {"metric": MEDIA_METRICS})
    return {m["name"]: (m.get("values") or [{}])[0].get("value", m.get("total_value", {}).get("value"))
            for m in body.get("data", [])}


@mcp.tool()
def threads_my_insights(since: str | None = None, until: str | None = None) -> dict[str, Any]:
    """Account-level insights for the token owner (views, likes, replies, reposts, quotes, followers)."""
    body = _get("me/threads_insights", {"metric": USER_METRICS, "since": since, "until": until})
    out: dict[str, Any] = {}
    for m in body.get("data", []):
        if "total_value" in m:
            out[m["name"]] = m["total_value"].get("value")
        else:
            out[m["name"]] = m.get("values")
    return out


# ---------------------------------------------------------------- CLI

def _setup() -> None:
    print("Paste a Threads user access token (from the Threads use case's User Token Generator")
    print("in the Meta app dashboard). Input is hidden.")
    token = getpass.getpass("Token: ").strip()
    if not token:
        sys.exit("No token entered.")
    me = _raw_get("me", {"fields": "id,username", "access_token": token})
    print(f"Token works for @{me.get('username')} (id {me.get('id')}).")

    expires_at = None
    secret = getpass.getpass(
        "Threads app secret, to upgrade a 1-hour token to a 60-day one (Enter to skip): "
    ).strip()
    if secret:
        try:
            body = _raw_get(
                "access_token",
                {"grant_type": "th_exchange_token", "client_secret": secret, "access_token": token},
            )
            token = body["access_token"]
            expires_at = time.time() + int(body.get("expires_in", 0))
            print("Exchanged for a long-lived token.")
        except ThreadsError as e:
            print(f"Exchange skipped ({e}); the token may already be long-lived.")
    _save_token({"access_token": token, "obtained_at": time.time(), "expires_at": expires_at})
    print(f"Saved to {TOKEN_FILE}. The app secret was not stored.")


def _status_cli() -> None:
    print(json.dumps(threads_status(), indent=2))
    for name, fn in [
        ("keyword_search (own posts unless approved)", lambda: threads_keyword_search("the", limit=1)),
        ("profile_posts @threads", lambda: threads_profile_posts("threads", limit=1)),
    ]:
        try:
            r = fn()
            print(f"OK   {name}: {r['count']} row(s)")
        except ThreadsError as e:
            print(f"FAIL {name}: {e}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    try:
        if cmd == "setup":
            _setup()
        elif cmd == "status":
            _status_cli()
        else:
            mcp.run()
    except ThreadsError as e:
        sys.exit(str(e))
