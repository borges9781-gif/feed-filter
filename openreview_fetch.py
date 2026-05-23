"""
openreview_fetch.py
Pull submissions from OpenReview venues, filter with the same
keywords used in filter.py, emit public/openreview.xml.
"""
import os
import re
import sys
import yaml
import time
from pathlib import Path
from datetime import datetime, timezone
from feedgen.feed import FeedGenerator

import openreview

ROOT = Path(__file__).parent
MAIN_CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
OR_CFG   = yaml.safe_load((ROOT / "openreview_config.yaml").read_text(encoding="utf-8"))


def compile_patterns(words, case_sensitive=False):
    flags = 0 if case_sensitive else re.IGNORECASE
    out = []
    for kw in words:
        if any(c in kw for c in (" ", "-", ".")):
            out.append((kw, re.compile(re.escape(kw), flags)))
        else:
            out.append((kw, re.compile(rf"\b{re.escape(kw)}\b", flags)))
    return out


PATTERNS = (compile_patterns(MAIN_CFG.get("keywords", []), False)
            + compile_patterns(MAIN_CFG.get("case_sensitive_keywords", []), True))


def get_value(content, field, default=""):
    """V2 wraps content fields in {'value': ...}; tolerate both."""
    v = content.get(field)
    if v is None:
        return default
    if isinstance(v, dict) and "value" in v:
        v = v["value"]
    if isinstance(v, list):
        v = " ".join(str(x) for x in v)
    return str(v)


def fetch_venue(client, venue_id, patterns):
    for pat in patterns:
        inv = pat.format(vid=venue_id)
        try:
            notes = client.get_all_notes(invitation=inv)
            if notes:
                return notes, inv
        except Exception:
            continue
    return [], None


def main():
    user = os.environ.get("OPENREVIEW_USERNAME")
    pwd  = os.environ.get("OPENREVIEW_PASSWORD")
    try:
        client = openreview.api.OpenReviewClient(
            baseurl="https://api2.openreview.net",
            username=user, password=pwd,
        )
    except Exception as e:
        print(f"Auth failed, falling back to anonymous: {e}")
        client = openreview.api.OpenReviewClient(baseurl="https://api2.openreview.net")

    inv_patterns = OR_CFG.get("invitation_patterns",
                              ["{vid}/-/Submission", "{vid}/-/Blind_Submission"])

    seen, hits, stats = set(), [], []

    for venue in OR_CFG["venues"]:
        vid, name = venue["id"], venue["name"]
        t0 = time.time()
        notes, used_inv = fetch_venue(client, vid, inv_patterns)
        total = matched = 0
        for note in notes:
            total += 1
            if note.id in seen:
                continue
            seen.add(note.id)

            title    = get_value(note.content, "title")
            abstract = get_value(note.content, "abstract")
            kwfield  = get_value(note.content, "keywords")
            text = f"{title} {abstract} {kwfield}"

            kws = [kw for kw, p in PATTERNS if p.search(text)]
            if not kws:
                continue

            matched += 1
            hits.append({
                "id":      note.id,
                "title":   title,
                "abstract":abstract,
                "url":     f"https://openreview.net/forum?id={note.id}",
                "venue":   name,
                "matched": kws,
                "cdate":   getattr(note, "cdate", None) or 0,
            })

        elapsed = time.time() - t0
        stats.append((name, total, matched, used_inv or "NO_MATCH", f"{elapsed:.1f}s"))

    hits.sort(key=lambda h: h["cdate"], reverse=True)
    hits = hits[: OR_CFG.get("max_items", 200)]

    fg = FeedGenerator()
    fg.id("feed-filter:openreview")
    fg.title("Filtered OpenReview Submissions")
    fg.link(href="https://example.com/openreview.xml", rel="self")
    fg.description(
        f"Filtered OpenReview submissions from "
        f"{len(OR_CFG['venues'])} venues; {len(hits)} matches."
    )
    fg.language("en")

    for h in hits:
        fe = fg.add_entry()
        fe.id(h["id"])
        tag = " | ".join(h["matched"][:3])
        fe.title(f"[{h['venue']}] [{tag}] {h['title']}")
        fe.link(href=h["url"])
        kw_block = (
            f"<p><b>Venue:</b> {h['venue']}<br>"
            f"<b>Matched:</b> {', '.join(h['matched'])}</p>"
        )
        fe.description(kw_block + h["abstract"])
        if h["cdate"]:
            fe.pubDate(datetime.fromtimestamp(h["cdate"] / 1000, tz=timezone.utc))

    out = ROOT / "public"
    out.mkdir(exist_ok=True)
    fg.rss_file(str(out / "openreview.xml"))

    print("=" * 90)
    print(f"{'venue':<28} {'total':>6} {'hits':>5} {'invitation':<40} {'time':>6}")
    print("-" * 90)
    for name, total, matched, inv, t in stats:
        inv_short = inv if len(inv) < 38 else inv[:35] + "..."
        print(f"{name:<28} {total:>6} {matched:>5} {inv_short:<40} {t:>6}")
    print(f"Wrote {len(hits)} items to public/openreview.xml")


if __name__ == "__main__":
    main()
