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
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

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
USER_METRICS = "views,likes,replies,reposts,quotes"
FOLLOWERS_METRIC = "followers_count"  # rejected together with since/until

REFRESH_AFTER_SECONDS = 3 * 24 * 3600  # refresh well inside the 60-day lifetime
RETRY_REFRESH_SECONDS = 3600  # after a failed refresh, try again an hour later


class ThreadsError(ToolError):
    """Expected API/token failure; its message reaches the model as-is."""


# ---------------------------------------------------------------- HTTP + token

_SECRET_RE = re.compile(r"((?:access_token|client_secret)=)[^&\s\"']+")


def _scrub(msg: str, secrets: list[str]) -> str:
    """Remove tokens and secrets from text that may reach the model or logs."""
    for s in secrets:
        if s:
            msg = msg.replace(s, "***")
    return _SECRET_RE.sub(r"\1***", msg)


def _raw_get(path: str, params: dict[str, Any] | None = None, secrets: list[str] | None = None) -> dict[str, Any]:
    url = path if path.startswith("http") else f"{API_BASE}/{path.lstrip('/')}"
    params = params or {}
    secrets = [str(params[k]) for k in ("access_token", "client_secret") if params.get(k)] + (secrets or [])
    try:
        resp = httpx.get(url, params=params or None, timeout=30)
    except httpx.HTTPError as e:
        raise ThreadsError(_scrub(f"Network error ({type(e).__name__}): {e}", secrets)) from None
    try:
        body = resp.json()
    except ValueError:
        raise ThreadsError(_scrub(f"HTTP {resp.status_code}: {resp.text[:300]}", secrets)) from None
    if resp.status_code >= 400 or "error" in body:
        err = body.get("error", body)
        raise ThreadsError(_scrub(
            f"HTTP {resp.status_code}: {err.get('message', err)} "
            f"(type={err.get('type')}, code={err.get('code')})",
            secrets,
        ))
    return body


def _load_token() -> dict[str, Any]:
    if not TOKEN_FILE.exists():
        raise ThreadsError(
            f"No Threads token at {TOKEN_FILE}. Run: uv run --script server.py setup"
        )
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))


def _save_token(data: dict[str, Any]) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, TOKEN_FILE)  # atomic, so a crash never leaves half a file


_token_lock = threading.Lock()


def _access_token() -> str:
    # Tools run in worker threads; the lock stops two calls refreshing at once.
    with _token_lock:
        tok = _load_token()
        now = time.time()
        age = now - tok.get("obtained_at", 0)
        since_attempt = now - tok.get("last_refresh_attempt", 0)
        if age > REFRESH_AFTER_SECONDS and since_attempt > RETRY_REFRESH_SECONDS:
            tok["last_refresh_attempt"] = now
            try:
                body = _raw_get(
                    "refresh_access_token",
                    {"grant_type": "th_refresh_token", "access_token": tok["access_token"]},
                )
                expires_in = body.get("expires_in")
                tok.update(
                    access_token=body["access_token"],
                    obtained_at=now,
                    expires_at=now + int(expires_in) if expires_in else None,
                    last_refresh_error=None,
                )
            except ThreadsError as e:
                # Not fatal while the old token is still valid; surface it in status.
                tok["last_refresh_error"] = str(e)
            _save_token(tok)
        return tok["access_token"]


def _ts(value: str | None) -> str | None:
    """Turn YYYY-MM-DD into a Unix timestamp (some endpoints only take those)."""
    if value and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return str(int(dt.timestamp()))
    return value


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    for key in ("since", "until"):
        if key in params:
            params[key] = _ts(params[key])
    params["access_token"] = _access_token()
    return _raw_get(path, params)


def _paged(path: str, params: dict[str, Any], pages: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    body = _get(path, params)
    rows.extend(body.get("data", []))
    for _ in range(max(pages, 1) - 1):
        if not body.get("data"):
            break  # an empty page can still carry a next link; don't spend quota on it
        paging = body.get("paging", {})
        after = paging.get("cursors", {}).get("after")
        if paging.get("next"):
            # The next URL already carries every param, including the token.
            body = _raw_get(paging["next"], secrets=[_access_token()])
        elif after:
            body = _get(path, {**params, "after": after})
        else:
            break
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


def _metric_value(m: dict[str, Any]) -> Any:
    """Insights come back as total_value or as a values series; keep series whole."""
    total = m.get("total_value")
    if isinstance(total, dict):
        return total.get("value")
    values = m.get("values") or []
    if len(values) == 1:
        return values[0].get("value")
    return values


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
    return {m["name"]: _metric_value(m) for m in body.get("data", [])}


@mcp.tool()
def threads_my_insights(since: str | None = None, until: str | None = None) -> dict[str, Any]:
    """Account-level insights for the token owner (views, likes, replies, reposts, quotes,
    and followers_count when no date range is given). Dates must be on or after 2024-04-13."""
    metrics = USER_METRICS if (since or until) else f"{USER_METRICS},{FOLLOWERS_METRIC}"
    body = _get("me/threads_insights", {"metric": metrics, "since": since, "until": until})
    return {m["name"]: _metric_value(m) for m in body.get("data", [])}


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
    exchanged = False
    if secret:
        try:
            body = _raw_get(
                "access_token",
                {"grant_type": "th_exchange_token", "client_secret": secret, "access_token": token},
            )
            token = body["access_token"]
            expires_at = time.time() + int(body.get("expires_in", 0))
            exchanged = True
            print("Exchanged for a long-lived (60-day) token.")
        except ThreadsError as e:
            print(f"Exchange failed ({e}).")
    if not exchanged:
        print("WARNING: token not exchanged. Tokens from the User Token Generator are already")
        print("60-day tokens and will refresh themselves. A 1-hour token (e.g. from the OAuth")
        print("flow) will die within the hour; rerun setup with the app secret in that case.")
    _save_token({"access_token": token, "obtained_at": time.time(), "expires_at": expires_at})
    print(f"Saved to {TOKEN_FILE}. The app secret was not stored.")


def _status_cli() -> None:
    print(json.dumps(threads_status(), indent=2, ensure_ascii=False))
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
            sys.stdout.reconfigure(encoding="utf-8")
            _setup()
        elif cmd == "status":
            sys.stdout.reconfigure(encoding="utf-8")
            _status_cli()
        else:
            mcp.run()
    except ThreadsError as e:
        sys.exit(str(e))
