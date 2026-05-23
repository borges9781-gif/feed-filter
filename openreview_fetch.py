"""
openreview_fetch.py — pull submissions from OpenReview venues + workshops,
filter with the SHARED keyword set (config.yaml), select with the SHARED
round-robin rule (common.round_robin_select), emit public/openreview.xml.

Source granularity for round-robin = each venue / workshop, so a huge venue
(NeurIPS main, thousands of papers) cannot crowd out a small workshop.
"""
import os
import re
import time
import yaml
from pathlib import Path
from datetime import datetime, timezone
from feedgen.feed import FeedGenerator

import openreview

import common

ROOT     = Path(__file__).parent
MAIN_CFG = common.load_main_config()
OR_CFG   = yaml.safe_load((ROOT / "openreview_config.yaml").read_text(encoding="utf-8"))

PATTERNS = common.build_patterns(MAIN_CFG)

INV_PATTERNS      = OR_CFG.get("invitation_patterns",
                               ["{vid}/-/Submission", "{vid}/-/Blind_Submission"])
EXCLUDE_WITHDRAWN = OR_CFG.get("exclude_withdrawn", True)
PER_SOURCE_CAP    = OR_CFG.get("per_source_cap", 60)
MAX_ITEMS         = OR_CFG.get("max_items", 300)

WITHDRAW_RE = re.compile(r"(withdrawn|desk[\s_-]*reject|rejected)", re.IGNORECASE)
ACCEPT_RE   = re.compile(r"(accept|oral|spotlight|poster)", re.IGNORECASE)


def get_value(content, field, default=""):
    """API2 wraps content fields as {'value': ...}; tolerate both shapes."""
    v = content.get(field)
    if v is None:
        return default
    if isinstance(v, dict) and "value" in v:
        v = v["value"]
    if isinstance(v, list):
        v = " ".join(str(x) for x in v)
    return str(v)


def get_status(note):
    """Derive accept/withdraw status from venue / venueid fields if present."""
    for fld in ("venueid", "venue"):
        v = get_value(note.content, fld)
        if v:
            low = v.lower()
            if ACCEPT_RE.search(low) or WITHDRAW_RE.search(low):
                return v
    return ""


def fetch_notes(client, vid):
    """Try invitation patterns, then fall back to a venueid content query.
    Returns (notes, how) where `how` describes which strategy worked."""
    for pat in INV_PATTERNS:
        inv = pat.format(vid=vid)
        try:
            notes = client.get_all_notes(invitation=inv)
            if notes:
                return notes, inv
        except Exception:
            continue
    # Fallback: accepted papers carry venueid == vid
    try:
        notes = client.get_all_notes(content={"venueid": vid})
        if notes:
            return notes, f"content.venueid={vid}"
    except Exception:
        pass
    return [], "NO_MATCH"


def make_client():
    user = os.environ.get("OPENREVIEW_USERNAME")
    pwd  = os.environ.get("OPENREVIEW_PASSWORD")
    try:
        c = openreview.api.OpenReviewClient(
            baseurl="https://api2.openreview.net",
            username=user, password=pwd,
        )
        if user:
            print(f"OpenReview: authenticated as {user}")
        return c
    except Exception as e:
        print(f"OpenReview: auth failed ({e}); falling back to anonymous")
        return openreview.api.OpenReviewClient(baseurl="https://api2.openreview.net")


def main():
    client = make_client()

    targets = ([("venue", v) for v in OR_CFG.get("venues", [])]
               + [("workshop", w) for w in OR_CFG.get("workshops", [])])

    seen = set()
    hits_by_source = {}   # source_name -> [item dict]
    stats = []

    for kind, t in targets:
        vid, name = t["id"], t["name"]
        hits_by_source[name] = []
        t0 = time.time()
        notes, how = fetch_notes(client, vid)
        total = matched = skipped = 0

        for note in notes:
            total += 1
            if note.id in seen:
                continue
            seen.add(note.id)

            status = get_status(note)
            if EXCLUDE_WITHDRAWN and status and WITHDRAW_RE.search(status):
                skipped += 1
                continue

            title    = get_value(note.content, "title")
            abstract = get_value(note.content, "abstract")
            kwfield  = get_value(note.content, "keywords")
            text = f"{title} {abstract} {kwfield}"

            kws = common.match_text(text, PATTERNS)
            if not kws:
                continue

            matched += 1
            hits_by_source[name].append({
                "id":       note.id,
                "title":    title,
                "abstract": abstract,
                "url":      f"https://openreview.net/forum?id={note.id}",
                "venue":    name,
                "kind":     kind,
                "status":   status,
                "matched":  kws,
                "_published_dt": common.ms_to_dt(getattr(note, "cdate", None)),
            })

        # cap per source before round-robin
        hits_by_source[name].sort(key=lambda h: h["_published_dt"], reverse=True)
        hits_by_source[name] = hits_by_source[name][:PER_SOURCE_CAP]

        stats.append((kind, name, total, matched, skipped, how, f"{time.time()-t0:.1f}s"))

    # SHARED round-robin: fair across venues/workshops
    all_hits = common.round_robin_select(hits_by_source, MAX_ITEMS)
    all_hits.sort(key=lambda h: h["_published_dt"], reverse=True)

    # ----- Build output feed -----
    fg = FeedGenerator()
    fg.id("feed-filter:openreview")
    fg.title("Filtered OpenReview (Main + Workshops)")
    fg.link(href="https://example.com/openreview.xml", rel="self")
    fg.description(
        f"Filtered from {len(OR_CFG.get('venues', []))} venues + "
        f"{len(OR_CFG.get('workshops', []))} workshops; {len(all_hits)} matches."
    )
    fg.language("en")

    for h in all_hits:
        fe = fg.add_entry()
        fe.id(h["id"])
        tag      = " | ".join(h["matched"][:3])
        status_t = f" {{{h['status']}}}" if h["status"] else ""
        wkflag   = " [W]" if h["kind"] == "workshop" else ""
        fe.title(f"[{h['venue']}{wkflag}{status_t}] [{tag}] {h['title']}")
        fe.link(href=h["url"])
        meta = (
            f"<p><b>Venue:</b> {h['venue']} ({h['kind']})<br>"
            f"<b>Status:</b> {h['status'] or 'in review'}<br>"
            f"<b>Matched:</b> {', '.join(h['matched'])}</p>"
        )
        fe.description(meta + h["abstract"])
        fe.pubDate(h["_published_dt"])

    out = ROOT / "public"
    out.mkdir(exist_ok=True)
    fg.rss_file(str(out / "openreview.xml"))

    # ----- Stats -----
    print("=" * 104)
    print(f"{'kind':<9} {'venue':<32} {'total':>6} {'hits':>5} {'skip':>5} {'strategy':<30} {'time':>6}")
    print("-" * 104)
    for kind, name, total, matched, skipped, how, t in stats:
        how_s = how if len(how) < 28 else how[:25] + "..."
        print(f"{kind:<9} {name:<32} {total:>6} {matched:>5} {skipped:>5} {how_s:<30} {t:>6}")
    print("-" * 104)
    print(f"Wrote {len(all_hits)} items to public/openreview.xml")


if __name__ == "__main__":
    main()
