#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["yt-dlp[default,curl-cffi]"]
# ///
"""TikTok account report: public profile and recent-video stats for a list of accounts.

Writes to out/:
  report.md       — report for bots / LLMs with every video (paste into a chat as is)
  report_brief.md — the same without per-video tables, when a bot needs it shorter
  report.json     — the same as structured data, for scripts
  dashboard.html  — visual dashboard
  history/*.json  — one snapshot per run (deltas are computed from these)

    uv run tiktok_stats.py                 # all accounts from accounts.txt
    uv run tiktok_stats.py user1 @user2    # only these accounts
    uv run tiktok_stats.py --md            # also print report.md to stdout (--brief for the short one)
    uv run tiktok_stats.py --cached        # rebuild reports from the latest snapshot, no requests
    uv run tiktok_stats.py --open          # open dashboard.html when done
    uv run tiktok_stats.py --demo --open   # dashboard from synthetic data, no network
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACCOUNTS_FILE = ROOT / "accounts.txt"
OUT_DIR = ROOT / "out"
HISTORY_DIR = OUT_DIR / "history"
TEMPLATE = ROOT / "dashboard_template.html"
NOTES_FILE = ROOT / "notes.txt"

DEFAULT_VIDEOS = 30
STALE_DAYS = 3      # this many days without posts -> signal
LOW_VIEWS = 200     # a video older than a day with fewer views -> signal
WORKERS = 3

NOTE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):?\s+(?:@([\w.\-]+):?\s+)?(.+)$")
PROFILE_FIELDS = (("nickname", "name"), ("bio", "bio"), ("bio_link", "bio link"), ("private", "private"))

UNIVERSAL_RE = re.compile(
    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)


class FetchError(Exception):
    pass


class NotFound(FetchError):
    pass


# ---------------------------------------------------------------- accounts

def parse_handle(raw: str) -> str | None:
    s = raw.split("#", 1)[0].strip()
    if not s:
        return None
    m = re.search(r"tiktok\.com/@([\w.\-]+)", s)
    if m:
        s = m.group(1)
    return s.strip(" \t\"'«»@").lower() or None


def load_accounts(items: list[str]) -> list[str]:
    if not items:
        if not ACCOUNTS_FILE.exists():
            sys.exit(f"No {ACCOUNTS_FILE.name}: create it with one account per line "
                     "(see accounts.example.txt).")
        items = ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines()
    handles = [h for h in map(parse_handle, items) if h]
    return list(dict.fromkeys(handles))


def parse_notes(lines: list[str]) -> list[dict]:
    """Log lines: “2026-10-03 @handle: what was done” or “2026-10-03: general note”."""
    notes = []
    for line in lines:
        m = NOTE_RE.match(line.strip())
        if m:
            notes.append({"date": m[1], "handle": (m[2] or "").lower() or None, "text": m[3].strip()})
    return sorted(notes, key=lambda x: x["date"])


def load_notes() -> list[dict]:
    if not NOTES_FILE.exists():
        return []
    return parse_notes(NOTES_FILE.read_text(encoding="utf-8").splitlines())


# ---------------------------------------------------------------- fetching

def fetch_profile(handle: str) -> dict:
    from curl_cffi import requests as http

    last_err: Exception | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(2 * attempt)
        try:
            r = http.get(f"https://www.tiktok.com/@{handle}", impersonate="chrome",
                         headers={"Accept-Language": "en-US,en;q=0.9"}, timeout=30)
            m = UNIVERSAL_RE.search(r.text)
            if not m:
                raise FetchError(f"HTTP {r.status_code}: no profile data on the page (captcha or block)")
            detail = json.loads(m.group(1))["__DEFAULT_SCOPE__"].get("webapp.user-detail") or {}
            info = detail.get("userInfo") or {}
            user = info.get("user") or {}
            if not user.get("uniqueId"):
                code = detail.get("statusCode")
                if code == 10221:
                    raise NotFound("account not found or banned")
                raise FetchError(f"profile unavailable (statusCode={code} {detail.get('statusMsg') or ''})".strip())
            stats = {**(info.get("stats") or {}), **(info.get("statsV2") or {})}
            num = lambda k, s=stats: int(s.get(k) or 0)
            return {
                "handle": user["uniqueId"],
                "nickname": user.get("nickname") or "",
                "bio": (user.get("signature") or "").strip(),
                "bio_link": (user.get("bioLink") or {}).get("link"),
                "verified": bool(user.get("verified")),
                "private": bool(user.get("privateAccount")),
                "created_ts": user.get("createTime"),
                "sec_uid": user.get("secUid"),
                "followers": num("followerCount"),
                "following": num("followingCount"),
                "likes": num("heartCount"),
                "videos": num("videoCount"),
            }
        except NotFound:
            raise
        except Exception as e:  # noqa: BLE001 — network, block, broken JSON: try again
            last_err = e
    raise FetchError(str(last_err))


class _SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _entries(url: str, limit: int) -> list[dict]:
    from yt_dlp import YoutubeDL

    opts = {"quiet": True, "no_warnings": True, "logger": _SilentLogger(),
            "extract_flat": "in_playlist", "playlistend": limit, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return [e for e in info.get("entries") or [] if e and e.get("id")]


def current_handle(sec_uid: str) -> str | None:
    """Current handle by secUid, which survives handle changes. Taken from the latest video."""
    try:
        entries = _entries(f"tiktokuser:{sec_uid}", 1)
    except Exception:  # noqa: BLE001
        return None
    return ((entries[0].get("uploader") or "").lower() or None) if entries else None


def fetch_videos(handle: str, sec_uid: str | None, limit: int) -> list[dict]:
    url = f"tiktokuser:{sec_uid}" if sec_uid else f"https://www.tiktok.com/@{handle}"
    videos = []
    for e in _entries(url, limit):
        videos.append({
            "id": str(e["id"]),
            "ts": e.get("timestamp"),
            "duration": e.get("duration"),
            "caption": (e.get("description") or e.get("title") or "").strip(),
            "views": int(e.get("view_count") or 0),
            "likes": int(e.get("like_count") or 0),
            "comments": int(e.get("comment_count") or 0),
            "shares": int(e.get("repost_count") or 0),
            "saves": int(e.get("save_count") or 0),
        })
    videos.sort(key=lambda v: v["ts"] or 0, reverse=True)
    return videos[:limit]


def collect(handle: str, limit: int, sec_uid: str | None = None) -> dict:
    acc = {"handle": handle, "fetched_ts": int(time.time()), "error": None,
           "profile": None, "videos": []}
    try:
        acc["profile"] = fetch_profile(handle)
    except FetchError as e:
        # the handle may have changed: secUid from earlier snapshots points to the same account
        new = current_handle(sec_uid) if sec_uid else None
        if not new or new == handle:
            acc["error"] = str(e)
            return acc
        try:
            acc["profile"] = fetch_profile(new)
        except FetchError as e2:
            acc["error"] = str(e2)
            return acc
        acc["renamed_from"] = handle
    acc["handle"] = acc["profile"]["handle"]
    if acc["profile"]["private"]:
        return acc
    try:
        acc["videos"] = fetch_videos(acc["handle"], acc["profile"]["sec_uid"], limit)
    except Exception as e:  # noqa: BLE001
        acc["error"] = f"could not fetch videos: {str(e).splitlines()[0][:200]}"
    return acc


# ---------------------------------------------------------------- history

def snapshot_files() -> list[Path]:
    return sorted(HISTORY_DIR.glob("*.json")) if HISTORY_DIR.exists() else []


def load_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def previous_accounts(handles: list[str], before: list[Path]) -> dict[str, dict]:
    """For each account, its latest successfully collected state from earlier snapshots."""
    found: dict[str, dict] = {}
    for path in reversed(before):
        if len(found) == len(handles):
            break
        for acc in load_snapshot(path).get("accounts", []):
            h = acc.get("handle")
            if h in handles and h not in found and acc.get("profile"):
                found[h] = acc
    return found


# ---------------------------------------------------------------- analysis

def ratio(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def engagement(v: dict) -> int:
    return v["likes"] + v["comments"] + v["shares"] + v["saves"]


def analyze(acc: dict, prev: dict | None, now: int, limit: int) -> dict:
    p = acc.get("profile") or {}
    vids = acc.get("videos") or []
    prev_p = (prev or {}).get("profile") or {}
    prev_views = {v["id"]: v["views"] for v in (prev or {}).get("videos") or []}

    for v in vids:
        v["url"] = f"https://www.tiktok.com/@{acc['handle']}/video/{v['id']}"
        v["age_days"] = round((now - v["ts"]) / 86400, 2) if v.get("ts") else None
        v["er"] = ratio(engagement(v), v["views"])
        v["views_delta"] = v["views"] - prev_views[v["id"]] if v["id"] in prev_views else None

    def delta(key: str) -> int | None:
        return p[key] - prev_p[key] if p and prev_p and key in prev_p else None

    mature = [v for v in vids if (v["age_days"] or 0) >= 1]  # fresh videos are still growing — keep them out of medians
    mviews = [v["views"] for v in mature]
    in_days = lambda d: [v for v in vids if v["age_days"] is not None and v["age_days"] <= d]  # noqa: E731
    last7, last30 = in_days(7), in_days(30)
    total_views = sum(v["views"] for v in vids)

    recent, rest = mviews[:5], mviews[5:]
    med_recent = round(statistics.median(recent)) if len(recent) >= 3 else None
    med_rest = statistics.median(rest) if len(rest) >= 5 else None
    tracked = [v["views_delta"] for v in vids if v["views_delta"] is not None]
    new_since_prev = [v for v in vids if prev and v["id"] not in prev_views
                      and (v["ts"] or 0) > prev.get("fetched_ts", 0) - 600]

    m = {
        "followers": p.get("followers"),
        "followers_delta": delta("followers"),
        "likes": p.get("likes"),
        "likes_delta": delta("likes"),
        "videos_total": p.get("videos"),
        "videos_total_delta": delta("videos"),
        "prev_fetched_ts": (prev or {}).get("fetched_ts"),
        "videos_analyzed": len(vids),
        "last_post_ts": vids[0]["ts"] if vids else None,
        "days_since_last_post": vids[0]["age_days"] if vids else None,
        "posts_7d": len(last7),
        "posts_30d": len(last30),
        "posts_30d_is_lower_bound": bool(vids) and len(last30) == len(vids) and len(vids) >= limit,
        "views_7d": sum(v["views"] for v in last7),
        "views_30d": sum(v["views"] for v in last30),
        "views_total_analyzed": total_views,
        "median_views": round(statistics.median(mviews)) if mviews else None,
        "avg_views": round(statistics.mean(mviews)) if mviews else None,
        "max_views": max(mviews) if mviews else None,
        "er": ratio(sum(engagement(v) for v in vids), total_views),
        "hit_rate_1k": ratio(sum(x >= 1_000 for x in mviews), len(mviews)),
        "hit_rate_10k": ratio(sum(x >= 10_000 for x in mviews), len(mviews)),
        "median_views_last5": med_recent,
        "trend_last5": ratio(med_recent, med_rest) if med_recent is not None and med_rest else None,
        "views_gained": (sum(tracked) + sum(v["views"] for v in new_since_prev)) if prev else None,
        "top_videos": [v["id"] for v in sorted(vids, key=lambda v: -v["views"])[:3]],
        "growing_now": [v["id"] for v in sorted((v for v in vids if v["views_delta"]),
                                                key=lambda v: -v["views_delta"])[:3]],
    }
    m["profile_changes"] = [
        {"field": label, "old": prev_p.get(key), "new": p.get(key)}
        for key, label in PROFILE_FIELDS if p and prev_p and prev_p.get(key) != p.get(key)]
    m["flags"] = flags(acc, m, mature)
    return m


def flags(acc: dict, m: dict, mature: list[dict]) -> list[dict]:
    out = []
    add = lambda level, text: out.append({"level": level, "text": text})  # noqa: E731
    p = acc.get("profile") or {}
    if acc.get("error"):
        add("critical", f"Collection failed: {acc['error']}")
    if p.get("private"):
        add("critical", "Account is private — videos are hidden")
    if not p or p.get("private"):
        return out
    if acc.get("renamed_from"):
        add("info", f"Handle changed: @{acc['renamed_from']} → @{acc['handle']}. Update accounts.txt")
    show = lambda x: ("yes" if x else "no") if isinstance(x, bool) else f"“{cell(x, 80)}”" if x else "empty"  # noqa: E731
    for c in m["profile_changes"]:
        add("info", f"Profile {c['field']} changed: {show(c['old'])} → {show(c['new'])}")
    if not acc.get("videos") and not acc.get("error"):
        add("warning", "No published videos")
    d = m["days_since_last_post"]
    if d is not None and d >= STALE_DAYS:
        add("warning", f"No posts for {int(d)} days")
    floor = max(LOW_VIEWS, round(0.3 * (m["median_views"] or 0)))
    stuck = [v for v in mature if v["age_days"] <= 7 and v["views"] < floor]
    if stuck:
        add("warning", f"{len(stuck)} video(s) from the past week under {floor} views after 24 h "
                       "(did not reach recommendations)")
    t = m["trend_last5"]
    if t is not None and t < 0.4:
        add("warning", f"Drop: median of the last 5 videos is {round(t * 100)}% of the earlier ones")
    elif t is not None and t >= 2:
        add("good", f"Growth: median of the last 5 videos is ×{t:.1f} the earlier ones")
    if (m["followers_delta"] or 0) < 0:
        add("info", f"{-m['followers_delta']} fewer followers since the previous snapshot")
    return out


def build_report(snapshot: dict, prev_by_handle: dict[str, dict], limit: int,
                 notes: list[dict] | None = None) -> dict:
    now = snapshot["generated_ts"]
    notes = load_notes() if notes is None else notes
    accounts = []
    for acc in snapshot["accounts"]:
        acc = json.loads(json.dumps(acc))  # keep the snapshot intact
        prev = prev_by_handle.get(acc["handle"]) or prev_by_handle.get(acc.get("renamed_from"))
        acc["metrics"] = analyze(acc, prev, now, limit)
        acc["notes"] = [x for x in notes if x["handle"] and x["handle"] in (acc["handle"], acc.get("renamed_from"))]
        accounts.append(acc)
    ok = [a for a in accounts if a.get("profile")]
    s = lambda key: sum(a["metrics"].get(key) or 0 for a in ok)  # noqa: E731
    has_prev = any(a["metrics"]["prev_fetched_ts"] for a in ok)
    prev_ts = [a["metrics"]["prev_fetched_ts"] for a in ok if a["metrics"]["prev_fetched_ts"]]
    return {
        "generated_ts": now,
        "prev_ts": min(prev_ts) if prev_ts else None,
        "videos_per_account": limit,
        "totals": {
            "accounts": len(accounts),
            "accounts_ok": len(ok),
            "followers": s("followers"),
            "followers_delta": s("followers_delta") if has_prev else None,
            "posts_7d": s("posts_7d"),
            "views_7d": s("views_7d"),
            "views_30d": s("views_30d"),
            "views_gained": s("views_gained") if has_prev else None,
            "accounts_with_warnings": sum(
                any(f["level"] in ("warning", "critical") for f in a["metrics"]["flags"]) for a in accounts),
        },
        "notes": [x for x in notes if not x["handle"]],
        "accounts": accounts,
    }


# ---------------------------------------------------------------- demo data

DEMO_ACCOUNTS = (
    # handle, name, bio, followers, typical views, last-5 multiplier, days since last post, captions
    ("demo.kitchen", "Quick Kitchen", "15-minute dinners for busy weeknights 🍝", 48_200, 9_000, 2.6, 0.4,
     ("One-pan lemon garlic pasta", "3 sauces you can make in 5 minutes", "Crispy tofu without a fryer",
      "Weeknight ramen upgrade", "Meal prep for 4 days in 1 hour", "The only pancake recipe you need")),
    ("demo.trails", "Weekend Trails", "Hikes within 2 hours of the city", 12_900, 2_400, 0.25, 0.8,
     ("Sunrise ridge walk, 6 km loop", "Packing list for a day hike", "Hidden waterfall trail",
      "Beginner-friendly forest route", "What I eat on the trail", "Rainy day hike: worth it?")),
    ("demo.desk", "Desk Setup Lab", "Small upgrades for a better workspace", 3_150, 650, 1.0, 4.5,
     ("Cable management in 10 minutes", "Budget monitor arm test", "Desk lamp that saves your eyes",
      "Minimal setup tour", "Keyboard sound test", "3 desk gadgets under $20")),
)


def demo_data(now: int, limit: int = DEFAULT_VIDEOS) -> tuple[dict, dict[str, dict], list[dict]]:
    """Synthetic snapshot, previous snapshot (24 h earlier) and notes for --demo."""
    rnd = random.Random(42)
    prev_ts = now - 86400
    day = lambda offset: datetime.fromtimestamp(now - offset * 86400).strftime("%Y-%m-%d")  # noqa: E731
    accounts, prev_by_handle = [], {}
    for i, (handle, name, bio, followers, typical, recent_x, last_post, captions) in enumerate(DEMO_ACCOUNTS):
        videos, prev_videos = [], []
        for k in range(limit):
            ts = int(now - (last_post + k * 1.3 + rnd.uniform(0, 0.4)) * 86400)
            age = (now - ts) / 86400
            views = typical * rnd.lognormvariate(0, 0.35) * (recent_x if k < 6 else 1)
            views *= min(1.0, 0.3 + age * 0.7)  # videos under a day old are still gathering views
            views = int(views)
            v = {"id": f"7{i}{k:017d}", "ts": ts, "duration": rnd.randint(12, 58),
                 "caption": f"{captions[k % len(captions)]} #{handle.split('.')[1]}",
                 "views": views, "likes": int(views * rnd.uniform(0.05, 0.12)),
                 "comments": int(views * rnd.uniform(0.002, 0.006)), "shares": int(views * rnd.uniform(0.003, 0.01)),
                 "saves": int(views * rnd.uniform(0.005, 0.02))}
            videos.append(v)
            if ts < prev_ts - 600:
                growth = 0.35 if age < 3 else 0.06 if age < 10 else 0.01
                prev_videos.append({**v, "views": int(views * (1 - growth))})
        profile = {"handle": handle, "nickname": name, "bio": bio, "bio_link": None, "verified": False,
                   "private": False, "created_ts": now - 400 * 86400, "sec_uid": None, "followers": followers,
                   "following": 120 + i * 37, "likes": sum(v["likes"] for v in videos) * 4, "videos": 140 - i * 40}
        accounts.append({"handle": handle, "fetched_ts": now, "error": None, "profile": profile, "videos": videos})
        prev_profile = {**profile, "followers": followers - (410, -35, 6)[i], "likes": int(profile["likes"] * 0.985),
                        "videos": profile["videos"] - (len(videos) - len(prev_videos))}
        prev_by_handle[handle] = {"handle": handle, "fetched_ts": prev_ts, "profile": prev_profile,
                                  "videos": prev_videos}
    notes = [
        {"date": day(9), "handle": None, "text": "Moved posting time from morning to 7 pm"},
        {"date": day(6), "handle": "demo.kitchen", "text": "Switched to 20–30 s recipe cuts with on-screen steps"},
        {"date": day(5), "handle": "demo.trails", "text": "Tried long voice-over videos instead of music"},
    ]
    return {"generated_ts": now, "videos_per_account": limit, "accounts": accounts}, prev_by_handle, notes


# ---------------------------------------------------------------- output: markdown

def dt(ts: int | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts).strftime(fmt) if ts else "—"


def n(x) -> str:
    if x is None:
        return "—"
    return f"{x:,}" if isinstance(x, int) else f"{x:.1f}"


def sd(x) -> str:
    return "" if x is None else f"{x:+,}"


def pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}"


def pct_s(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def nd(x, d) -> str:
    """A number followed by its delta in parentheses, if there is one."""
    return n(x) if d is None else f"{n(x)} ({sd(d)})"


def cell(text: str, width: int = 70) -> str:
    t = re.sub(r"\s+", " ", text).replace("|", "/").strip()
    return (t[: width - 1] + "…") if len(t) > width else t


FLAG_ICON = {"critical": "⛔", "warning": "⚠️", "good": "✅", "info": "ℹ️"}


def render_md(rep: dict, brief: bool = False) -> str:
    t = rep["totals"]
    hours = (rep["generated_ts"] - rep["prev_ts"]) / 3600 if rep["prev_ts"] else None
    L = [
        "# TikTok: account report",
        "",
        f"Snapshot: {dt(rep['generated_ts'])} (local time). "
        + (f"Previous snapshot: {dt(rep['prev_ts'])} ({hours:.0f} h ago) — deltas (Δ) are computed against it."
           if hours is not None else "No previous snapshots yet — deltas (Δ) will appear from the next run."),
        f"Accounts: {t['accounts']}. The last {rep['videos_per_account']} videos of each account are analyzed.",
        "",
        "How to read: ER = (likes + comments + shares + saves) / views. "
        "Median, hit rate and trend use only videos older than 24 h. "
        "“Views 7d/30d” is the total views of videos published in the last 7/30 days. "
        "Trend = median of the last 5 videos / median of the rest. "
        "“Notes” is a log of what was done with the accounts: use it when looking for reasons behind growth or drops. "
        "Video link: https://www.tiktok.com/@<account>/video/<id>.",
        "",
        "## Totals",
        "",
        f"- Followers: {nd(t['followers'], t['followers_delta'])}",
        f"- Posts in the last 7 days: {t['posts_7d']}",
        f"- Views of videos from the last 7 days: {n(t['views_7d'])}; last 30 days: {n(t['views_30d'])}",
    ]
    if t["views_gained"] is not None:
        L.append(f"- Views gained since the previous snapshot: {n(t['views_gained'])}")
    L.append(f"- Accounts with signals: {t['accounts_with_warnings']} of {t['accounts']}")
    if rep["notes"]:
        L += ["", "## Notes (general)", ""] + [f"- {x['date']}: {x['text']}" for x in rep["notes"]]
    L += ["", "## Accounts", "",
          "| Account | Followers | Δ | Total likes | Posts 7d | Median views | Views 7d | ER % | Trend | Days since post | Signals |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for a in rep["accounts"]:
        m = a["metrics"]
        fl = " ".join(FLAG_ICON[f["level"]] for f in m["flags"]) or "—"
        trend = f"×{m['trend_last5']:.2f}" if m["trend_last5"] is not None else "—"
        L.append(f"| @{a['handle']} | {n(m['followers'])} | {sd(m['followers_delta'])} | {n(m['likes'])} | "
                 f"{m['posts_7d']} | {n(m['median_views'])} | {n(m['views_7d'])} | {pct(m['er'])} | {trend} | "
                 f"{n(m['days_since_last_post'])} | {fl} |")

    for a in rep["accounts"]:
        p, m = a.get("profile") or {}, a["metrics"]
        L += ["", f"## @{a['handle']}" + (f" — {p['nickname']}" if p.get("nickname") else ""), ""]
        if not p:
            L.append(f"No data: {a.get('error')}")
            continue
        badges = [b for b, on in (("verified", p["verified"]), ("private", p["private"])) if on]
        L.append(f"Profile: followers {nd(p['followers'], m['followers_delta'])}, "
                 f"following {n(p['following'])}, likes {nd(p['likes'], m['likes_delta'])}, "
                 f"videos {nd(p['videos'], m['videos_total_delta'])}"
                 + (f", created {dt(p['created_ts'], '%Y-%m-%d')}" if p.get("created_ts") else "")
                 + (f" ({', '.join(badges)})" if badges else "") + ".")
        if p.get("bio"):
            L.append(f"Bio: {cell(p['bio'], 200)}" + (f" · link: {p['bio_link']}" if p.get("bio_link") else ""))
        if a["videos"]:
            L.append(
                f"Videos analyzed: {m['videos_analyzed']}. Median {n(m['median_views'])}, "
                f"mean {n(m['avg_views'])}, max {n(m['max_views'])} views; ER {pct_s(m['er'])}; "
                f"≥1k views: {pct_s(m['hit_rate_1k'])}, ≥10k: {pct_s(m['hit_rate_10k'])}; "
                f"posts 7d/30d: {m['posts_7d']}/{m['posts_30d']}{'+' if m['posts_30d_is_lower_bound'] else ''}; "
                f"last post {n(m['days_since_last_post'])} days ago"
                + (f"; trend ×{m['trend_last5']:.2f}" if m["trend_last5"] is not None else "")
                + (f"; gained since the previous snapshot {n(m['views_gained'])}" if m["views_gained"] is not None else "")
                + ".")
        by_id = {v["id"]: v for v in a["videos"]}
        for title, ids, key in (("Top by views", m["top_videos"], "views"),
                                ("Fastest growing since the previous snapshot", m["growing_now"], "views_delta")):
            if ids:
                L.append(f"{title}: " + "; ".join(
                    f"{v['id']} ({sd(v[key]) if key == 'views_delta' else n(v[key])}, {dt(v['ts'], '%Y-%m-%d')}) “{cell(v['caption'], 50)}”"
                    for v in map(by_id.get, ids)))
        if m["flags"]:
            L += ["", "Signals:"] + [f"- {FLAG_ICON[f['level']]} {f['text']}" for f in m["flags"]]
        if a["notes"]:
            L += ["", "Notes:"] + [f"- {x['date']}: {x['text']}" for x in a["notes"]]
        if a["videos"] and not brief:
            L += ["", "| Date | Age, days | Length, s | Views | Δ views | Likes | Comments | Shares | Saves | ER % | Caption | id |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
            for v in a["videos"]:
                L.append(f"| {dt(v['ts'])} | {v['age_days']} | {n(v['duration'])} | {n(v['views'])} | "
                         f"{sd(v['views_delta'])} | {n(v['likes'])} | {n(v['comments'])} | {n(v['shares'])} | "
                         f"{n(v['saves'])} | {pct(v['er'])} | {cell(v['caption'])} | {v['id']} |")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- output: terminal & html

def compact(x) -> str:
    if x is None:
        return "—"
    for div, suf in ((1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"{x / div:.1f}".rstrip("0").rstrip(".") + suf
    return str(round(x))


def print_summary(rep: dict) -> None:
    rows = [("account", "followers", "Δ", "posts 7d", "median", "views 7d", "ER %", "last post", "")]
    for a in rep["accounts"]:
        m = a["metrics"]
        rows.append((
            "@" + a["handle"], compact(m["followers"]),
            ("+" if (m["followers_delta"] or 0) > 0 else "") + compact(m["followers_delta"]) if m["followers_delta"] is not None else "",
            str(m["posts_7d"]), compact(m["median_views"]), compact(m["views_7d"]), pct(m["er"]),
            f"{m['days_since_last_post']:.1f} d" if m["days_since_last_post"] is not None else "—",
            " ".join(FLAG_ICON[f["level"]] for f in m["flags"]),
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for i, r in enumerate(rows):
        print("  ".join(c.ljust(widths[j]) if j == 0 else c.rjust(widths[j]) for j, c in enumerate(r[:-1])), r[-1])
        if i == 0:
            print("  ".join("─" * w for w in widths[:-1]))
    for a in rep["accounts"]:
        for f in a["metrics"]["flags"]:
            if f["level"] != "good":
                print(f"  {FLAG_ICON[f['level']]} @{a['handle']}: {f['text']}")


def render_html(rep: dict) -> str:
    data = json.dumps(rep, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", data)


def write_outputs(rep: dict, out_dir: Path) -> tuple[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_md(rep)
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    (out_dir / "report_brief.md").write_text(render_md(rep, brief=True), encoding="utf-8")
    (out_dir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    dash = out_dir / "dashboard.html"
    dash.write_text(render_html(rep), encoding="utf-8")
    return md, dash


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="TikTok account report")
    ap.add_argument("accounts", nargs="*", help="accounts (defaults to accounts.txt)")
    ap.add_argument("-n", "--videos", type=int, default=DEFAULT_VIDEOS, help="how many recent videos to analyze")
    ap.add_argument("--cached", action="store_true", help="no TikTok requests: rebuild reports from the latest snapshot")
    ap.add_argument("--demo", action="store_true", help="build reports from synthetic data into out/demo, no network")
    ap.add_argument("--md", action="store_true", help="print report.md to stdout (for bots)")
    ap.add_argument("--brief", action="store_true", help="with --md: skip per-video tables (shorter for bots)")
    ap.add_argument("--json", action="store_true", help="print report.json to stdout")
    ap.add_argument("--open", action="store_true", help="open dashboard.html")
    args = ap.parse_args()
    log = (lambda *a: print(*a, file=sys.stderr)) if (args.md or args.json) else print

    out_dir = OUT_DIR
    if args.demo:
        snapshot, prev_by_handle, notes = demo_data(int(time.time()), args.videos)
        rep = build_report(snapshot, prev_by_handle, args.videos, notes)
        out_dir = OUT_DIR / "demo"
    else:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        history = snapshot_files()
        if args.cached:
            if not history:
                sys.exit("No snapshots yet — run without --cached first.")
            snapshot, before = load_snapshot(history[-1]), history[:-1]
            if args.accounts:
                wanted = set(load_accounts(args.accounts))
                snapshot["accounts"] = [a for a in snapshot["accounts"] if a["handle"] in wanted]
        else:
            handles = load_accounts(args.accounts)
            if not handles:
                sys.exit("The account list is empty: add accounts to accounts.txt or pass them as arguments.")
            log(f"[{datetime.now():%Y-%m-%d %H:%M}] Collecting {len(handles)} account(s), "
                f"last {args.videos} videos each…")
            started = time.time()
            sec_uids = {h: (a.get("profile") or {}).get("sec_uid") for h, a in previous_accounts(handles, history).items()}
            with ThreadPoolExecutor(WORKERS) as pool:
                accounts = list(pool.map(lambda h: collect(h, args.videos, sec_uids.get(h)), handles))
            snapshot = {"generated_ts": int(time.time()), "videos_per_account": args.videos, "accounts": accounts}
            path = HISTORY_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            before = history
            log(f"Done in {time.time() - started:.0f} s.")

        handles = [a["handle"] for a in snapshot["accounts"]]
        handles += [a["renamed_from"] for a in snapshot["accounts"] if a.get("renamed_from")]
        rep = build_report(snapshot, previous_accounts(handles, before), snapshot.get("videos_per_account", args.videos))

    md, dash = write_outputs(rep, out_dir)
    if args.md:
        print(render_md(rep, brief=True) if args.brief else md)
    elif args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print()
        print_summary(rep)
        print(f"\nReport for bots: {out_dir / 'report.md'}\nDashboard:       {dash}")
    if args.open:
        webbrowser.open(dash.as_uri())


if __name__ == "__main__":
    main()
