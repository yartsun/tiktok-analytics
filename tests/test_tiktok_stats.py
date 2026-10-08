import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tiktok_stats as ts  # noqa: E402

NOW = 1_760_000_000
DAY = 86400


def video(i, age_days, views, likes=0, comments=0, shares=0, saves=0):
    return {"id": str(i), "ts": NOW - int(age_days * DAY), "duration": 20, "caption": f"video {i}",
            "views": views, "likes": likes, "comments": comments, "shares": shares, "saves": saves}


def profile(**kw):
    p = {"handle": "acc", "nickname": "Acc", "bio": "", "bio_link": None, "verified": False, "private": False,
         "created_ts": None, "sec_uid": None, "followers": 1000, "following": 10, "likes": 5000, "videos": 20}
    return {**p, **kw}


def account(videos, **kw):
    return {"handle": "acc", "fetched_ts": NOW, "error": None, "profile": profile(**kw), "videos": videos}


def levels(m):
    return [f["level"] for f in m["flags"]]


def test_parse_handle():
    assert ts.parse_handle("@Some.User") == "some.user"
    assert ts.parse_handle("https://www.tiktok.com/@nasa?lang=en") == "nasa"
    assert ts.parse_handle("name  # comment") == "name"
    assert ts.parse_handle("# only a comment") is None
    assert ts.parse_handle("   ") is None


def test_parse_notes_splits_account_and_general_notes():
    notes = ts.parse_notes([
        "2026-10-05: general note",
        "# comment",
        "2026-10-03 @Acc: changed the bio",
    ])
    assert notes == [
        {"date": "2026-10-03", "handle": "acc", "text": "changed the bio"},
        {"date": "2026-10-05", "handle": None, "text": "general note"},
    ]


def test_analyze_core_metrics():
    vids = [video(0, 0.5, 100)] + [video(i, i, 1000, likes=100, comments=10) for i in range(1, 11)]
    m = ts.analyze(account(vids), None, NOW, 30)
    assert m["median_views"] == 1000  # the half-day-old video is excluded
    assert m["er"] == round(110 * 10 / (100 + 10_000), 4)
    assert m["posts_7d"] == 8
    assert m["days_since_last_post"] == 0.5
    assert m["trend_last5"] == 1.0
    assert m["views_gained"] is None  # no previous snapshot


def test_trend_flags_growth_and_drop():
    growing = [video(i, i + 1, 5000 if i < 5 else 1000) for i in range(12)]
    assert "good" in levels(ts.analyze(account(growing), None, NOW, 30))

    dropping = [video(i, i + 1, 300 if i < 5 else 1000) for i in range(12)]
    m = ts.analyze(account(dropping), None, NOW, 30)
    assert any(f["level"] == "warning" and f["text"].startswith("Drop") for f in m["flags"])


def test_stale_account_and_deltas_against_previous_snapshot():
    vids = [video(i, i + 4, 1000) for i in range(6)]
    prev = {"fetched_ts": NOW - DAY, "profile": profile(followers=1100, bio="old bio"),
            "videos": [{**v, "views": v["views"] - 40} for v in vids]}
    m = ts.analyze(account(vids, bio="new bio"), prev, NOW, 30)
    assert m["followers_delta"] == -100
    assert m["views_gained"] == 40 * 6
    texts = [f["text"] for f in m["flags"]]
    assert "No posts for 4 days" in texts
    assert "100 fewer followers since the previous snapshot" in texts
    assert any(t.startswith("Profile bio changed") for t in texts)


def test_private_account_is_critical_and_skips_other_checks():
    m = ts.analyze(account([], private=True), None, NOW, 30)
    assert levels(m) == ["critical"]


def test_demo_report_tells_a_different_story_per_account(tmp_path):
    snapshot, prev, notes = ts.demo_data(NOW)
    rep = ts.build_report(snapshot, prev, 30, notes)
    flags = {a["handle"]: a["metrics"]["flags"] for a in rep["accounts"]}
    assert [f["level"] for f in flags["demo.kitchen"]] == ["good"]
    assert any(f["text"].startswith("Drop") for f in flags["demo.trails"])
    assert any(f["text"].startswith("No posts") for f in flags["demo.desk"])
    assert rep["totals"]["accounts"] == 3 and rep["prev_ts"] == NOW - DAY

    md, dash = ts.write_outputs(rep, tmp_path)
    assert md.startswith("# TikTok: account report")
    assert "## @demo.kitchen — Quick Kitchen" in md
    assert "| Date |" not in ts.render_md(rep, brief=True)
    html = dash.read_text(encoding="utf-8")
    assert "/*__DATA__*/null" not in html and "demo.trails" in html
    assert {p.name for p in tmp_path.iterdir()} == {"report.md", "report_brief.md", "report.json", "dashboard.html"}


def test_html_data_cannot_close_the_script_tag():
    snapshot, prev, notes = ts.demo_data(NOW)
    snapshot["accounts"][0]["profile"]["bio"] = "</script><script>alert(1)</script>"
    html = ts.render_html(ts.build_report(snapshot, prev, 30, notes))
    assert "</script><script>alert(1)" not in html
