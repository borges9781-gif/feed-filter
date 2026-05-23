"""
acl_fetch.py — fetch ACL Anthology volumes via BibTeX, filter with the SHARED
keyword set (config.yaml), select with the SHARED round-robin rule, emit
public/acl.xml.

Source granularity for round-robin = each volume, so a big main-conference
volume cannot crowd out a small Findings / workshop volume.
ACL Anthology has no per-paper timestamp, so within a volume we order by
number of matched keywords (more relevant first); recency is handled by
which volumes you list (newest years on top).
"""
import re
import time
import yaml
import requests
from pathlib import Path
from datetime import datetime, timezone
from feedgen.feed import FeedGenerator

import bibtexparser

import common

ROOT     = Path(__file__).parent
MAIN_CFG = common.load_main_config()
ACL_CFG  = yaml.safe_load((ROOT / "acl_config.yaml").read_text(encoding="utf-8"))

PATTERNS = common.build_patterns(MAIN_CFG)

URL_TPL        = ACL_CFG.get("bibtex_url_template",
                             "https://aclanthology.org/volumes/{vid}.bib")
PER_SOURCE_CAP = ACL_CFG.get("per_source_cap", 60)
MAX_ITEMS      = ACL_CFG.get("max_items", 300)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; feed-filter/1.0; +https://github.com/borges9781-gif/feed-filter)",
    "Accept": "application/x-bibtex, text/plain, */*;q=0.8",
}


def fetch_bib(vid, retries=3):
    url = URL_TPL.format(vid=vid)
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text, r.status_code
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(2 ** i)
    return "", last or "FETCH_ERROR"


def clean_braces(s):
    return s.replace("{", "").replace("}", "").replace("\n", " ").strip()


def parse_year(entry):
    try:
        return int(entry.get("year", "")) or 2024
    except Exception:
        return 2024


def main():
    targets = list(ACL_CFG.get("volumes", []))
    targets.extend(ACL_CFG.get("workshop_volumes", []))

    seen = set()
    hits_by_source = {}   # volume name -> [item dict]
    stats = []

    for v in targets:
        vid, name = v["id"], v["name"]
        hits_by_source[name] = []
        t0 = time.time()

        raw, note = fetch_bib(vid)
        if not raw or len(raw) < 200:
            stats.append((name, 0, 0, note if note != 200 else "EMPTY_OR_NOT_FOUND",
                          f"{time.time()-t0:.1f}s"))
            continue
        try:
            db = bibtexparser.loads(raw)
        except Exception as e:
            stats.append((name, 0, 0, f"PARSE_ERR: {e}", f"{time.time()-t0:.1f}s"))
            continue

        total = matched = 0
        for entry in db.entries:
            if entry.get("ENTRYTYPE", "").lower() == "proceedings":
                continue  # skip volume-level meta entry
            total += 1
            uid = entry.get("ID") or entry.get("url")
            if not uid or uid in seen:
                continue
            seen.add(uid)

            title    = clean_braces(entry.get("title", ""))
            abstract = clean_braces(entry.get("abstract", ""))
            text = f"{title} {abstract}"

            kws = common.match_text(text, PATTERNS)
            if not kws:
                continue

            matched += 1
            year = parse_year(entry)
            hits_by_source[name].append({
                "id":       uid,
                "title":    title,
                "abstract": abstract,
                "authors":  entry.get("author", "").replace(" and ", ", "),
                "url":      entry.get("url", f"https://aclanthology.org/{uid}"),
                "venue":    name,
                "matched":  kws,
                "year":     year,
                # no real per-paper timestamp; use Jan 1 of pub year for RSS validity
                "_published_dt": datetime(year, 1, 1, tzinfo=timezone.utc),
                # within-volume ordering key: more matched keywords first
                "_rank":    len(kws),
            })

        # cap per source: keep the most-relevant first
        hits_by_source[name].sort(key=lambda h: h["_rank"], reverse=True)
        hits_by_source[name] = hits_by_source[name][:PER_SOURCE_CAP]

        stats.append((name, total, matched,
                      "ok" if matched else "no-hit", f"{time.time()-t0:.1f}s"))

    # SHARED round-robin across volumes, ordering within volume by _rank
    all_hits = common.round_robin_select(hits_by_source, MAX_ITEMS, sort_key="_rank")
    # final display order: newest publication year first, then relevance
    all_hits.sort(key=lambda h: (h["year"], h["_rank"]), reverse=True)

    # ----- Build output feed -----
    fg = FeedGenerator()
    fg.id("feed-filter:acl")
    fg.title("Filtered ACL Anthology")
    fg.link(href="https://example.com/acl.xml", rel="self")
    fg.description(
        f"Filtered from {len(targets)} ACL Anthology volumes; {len(all_hits)} matches."
    )
    fg.language("en")

    for h in all_hits:
        fe = fg.add_entry()
        fe.id(h["id"])
        tag = " | ".join(h["matched"][:3])
        fe.title(f"[{h['venue']}] [{tag}] {h['title']}")
        fe.link(href=h["url"])
        meta = (
            f"<p><b>Venue:</b> {h['venue']}<br>"
            f"<b>Authors:</b> {h['authors']}<br>"
            f"<b>Matched:</b> {', '.join(h['matched'])}</p>"
        )
        fe.description(meta + h["abstract"])
        fe.pubDate(h["_published_dt"])

    out = ROOT / "public"
    out.mkdir(exist_ok=True)
    fg.rss_file(str(out / "acl.xml"))

    # ----- Stats -----
    print("=" * 80)
    print(f"{'volume':<32} {'total':>6} {'hits':>5} {'note':<18} {'time':>6}")
    print("-" * 80)
    for name, total, matched, note, t in stats:
        note_s = note if len(note) < 16 else note[:13] + "..."
        print(f"{name:<32} {total:>6} {matched:>5} {note_s:<18} {t:>6}")
    print("-" * 80)
    print(f"Wrote {len(all_hits)} items to public/acl.xml")


if __name__ == "__main__":
    main()
