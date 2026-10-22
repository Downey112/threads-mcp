# threads-mcp

A read-only MCP server for the official Threads (Meta) API, for analysing topics on Threads from Claude Code. It never posts, replies, likes or deletes anything.

## Tools

| Tool | What it does | Access it needs |
| --- | --- | --- |
| `threads_status` | Shows who the token belongs to and when it expires | `threads_basic` |
| `threads_keyword_search` | Searches posts by keyword or topic tag (`search_mode=TAG`), TOP or RECENT, with date range, media type and single-author filters | `threads_keyword_search`; **only your own posts until Meta approves it** |
| `threads_profile_lookup` | Shows a profile's bio and followers, plus likes, replies, reposts, quotes and views from the last 7 days | `threads_profile_discovery`; **only @meta, @threads, @instagram and @facebook until approved** |
| `threads_profile_posts` | Lists a public profile's posts | same as above |
| `threads_post_replies` | Gets the replies to a post (top level or the whole conversation) | `threads_read_replies` |
| `threads_get_post` | Fetches one post by ID | `threads_basic` |
| `threads_my_posts`, `threads_my_insights`, `threads_post_insights` | Your own posts and their stats | `threads_basic`, `threads_manage_insights` |

The list tools take `pages` to page through results (up to 100 rows per page). They also take `export_name`, which saves the rows to `exports/<name>.csv`.

Meta's limits: 2,200 keyword searches and 1,000 profile lookups per user per rolling 24 hours. Profile tools only return public accounts with at least 100 followers.

## One-time setup

1. **Create the app.** Go to https://developers.facebook.com/apps, choose Create app, and pick the **"Access the Threads API"** use case.
2. **Add permissions.** In the use case's settings, add `threads_basic`, `threads_keyword_search`, `threads_profile_discovery`, `threads_read_replies` and `threads_manage_insights`.
3. **Add yourself as a tester.** Under App roles → Roles, add your Threads account as a **Threads Tester**. Then accept the invite in the Threads app under Settings → Account → Website permissions → Invites.
4. **Generate a token.** Use the use case's **User Token Generator** and copy the token.
5. **Save the token.** Run this in your own terminal. The input is hidden, and the token is saved to `~/.threads-mcp/token.json`:
   ```
   uv run --script C:\Users\courts\threads-mcp\server.py setup
   ```
   Tokens from the User Token Generator already last 60 days, so you can press Enter at the app-secret prompt. If you have a 1-hour token instead (e.g. from your own OAuth flow), you **must** paste the app secret (App settings → Basic → Threads app secret), or the token dies within the hour. The script exchanges it for a 60-day token. The secret itself isn't stored.
6. **Check it works:** `uv run --script C:\Users\courts\threads-mcp\server.py status`
7. **Search everyone's posts.** To search all public posts and read other people's profiles, submit the app for **App Review** for `threads_keyword_search` and `threads_profile_discovery`. Meta asks for a description of the use case and a screencast. Until it's approved, those tools only return your own posts or Meta's own accounts.

The token refreshes itself once it's more than 3 days old, so it won't hit its 60-day expiry. If a refresh fails, it retries an hour later and `threads_status` shows the error. Tokens never appear in error messages or logs.

## Registered in Claude Code

```
claude mcp add threads --scope user -- uv run --script C:\Users\courts\threads-mcp\server.py
```
Restart Claude Code after adding it.
