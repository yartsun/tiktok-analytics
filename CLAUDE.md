# TikTok Analytics

Collects public stats for the TikTok accounts listed in `accounts.txt`. Details are in `README.md`.

When asked to analyze the accounts:
1. Fresh data: `uv run tiktok_stats.py --md`. If asked not to make new requests, or today's snapshot already exists: `uv run tiktok_stats.py --cached --md`.
2. Work from the report: changes since the previous snapshot, drops and growth, which videos (topics, captions, length) performed and why, posting consistency, how the accounts compare. Finish with concrete actions.
3. Snapshot history is in `out/history/`, the full data in `out/report.json`.
4. When the user mentions an event with an account (ban, format change, profile edit), add a line to `notes.txt`: `YYYY-MM-DD @handle: what happened`.

Code: `tiktok_stats.py` (collection via curl_cffi + yt-dlp, metrics, reports), `dashboard_template.html` (the dashboard; the data replaces `/*__DATA__*/null`). Tests: `uv run --no-project --with pytest pytest`.

Personal rules for account advice live in `CLAUDE.local.md`, which is git-ignored.
