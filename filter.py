"""
Aggregate-Filter-Republish for academic RSS/Atom feeds.
Reads config.yaml, fetches all feeds, filters by keywords,
emits a single combined RSS at public/filtered.xml.
"""
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; feed-filter/1.0; +https://github.com/borges9781-gif/feed-filter)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
}

import re
import sys
import yaml
import time
import feedparser
from pathlib import Path
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone

ROOT = Path(__file__).parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def compile_patterns(words, case_sensitive=False):
    """Build regex patterns. Phrases (with space/hyphen/dot) -> substring;
    single tokens -> word boundary. Returns list of (kw, regex)."""
    patterns = []
    flags = 0 if case_sensitive else re.IGNORECASE
    for kw in words:
        if any(ch in kw for ch in (" ", "-", ".")):
            pat = re.compile(re.escape(kw), flags)
        else:
            pat = re.compile(rf"\b{re.escape(kw)}\b", flags)
        patterns.append((kw, pat))
    return patterns


KW_CI = compile_patterns(CFG.get("keywords", []), case_sensitive=False)
KW_CS = compile_patterns(CFG.get("case_sensitive_keywords", []), case_sensitive=True)
ALL_PATTERNS = KW_CI + KW_CS

SEARCH_FIELDS = CFG.get("search_fields", ["title", "summary"])
MAX_ITEMS = CFG.get("max_items", 300)


def entry_text(entry):
    parts = []
    for f in SEARCH_FIELDS:
        v = entry.get(f, "")
        if isinstance(v, list):
            v = " ".join(str(x) for x in v)
        parts.append(str(v))
    return " ".join(parts)


def match(entry):
    """Return list of matched keywords, or [] if no match."""
    text = entry_text(entry)
    hits = [kw for kw, pat in ALL_PATTERNS if pat.search(text)]
    return hits


def parse_published(entry):
    for k in ("published_parsed", "updated_parsed"):
        v = entry.get(k)
        if v:
            try:
                return datetime(*v[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def round_robin_select(hits_by_feed, max_items):
    """Round-robin selection: take latest from each feed alternately.
    
    Algorithm:
    1. Sort each feed's hits by publish time (descending)
    2. Use round-robin to collect items: take one from each feed in turn
    3. Stop when max_items reached or all feeds exhausted
    
    This guarantees:
    - Every feed gets fair representation (no single feed can dominate)
    - Within each feed, newest items are prioritized
    - All feeds have opportunity to contribute, even small ones
    """
    feeds_with_hits = [(url, hits) for url, hits in hits_by_feed.items() if hits]
    if not feeds_with_hits:
        return []
    
    for url, hits in feeds_with_hits:
        hits.sort(key=lambda e: e["_published_dt"], reverse=True)
    
    result = []
    indices = {url: 0 for url, _ in feeds_with_hits}
    
    while len(result) < max_items:
        made_progress = False
        for url, _ in feeds_with_hits:
            if len(result) >= max_items:
                break
            idx = indices[url]
            if idx < len(hits_by_feed[url]):
                result.append(hits_by_feed[url][idx])
                indices[url] += 1
                made_progress = True
        if not made_progress:
            break
    
    return result


def main():
    seen_ids = set()
    hits_by_feed = {}
    stats = []

    for url in CFG["feeds"]:
        t0 = time.time()
        status, nbytes = "?", 0
        hits_by_feed[url] = []
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            status, nbytes = r.status_code, len(r.content)
            r.raise_for_status()
            d = feedparser.parse(r.content)
        except Exception as e:
            stats.append((url, 0, 0, f"FETCH_ERR {status} {nbytes}B: {e}"))
            continue
    
        if d.bozo and not d.entries:
            stats.append((url, 0, 0, f"BOZO {status} {nbytes}B: {d.bozo_exception}"))
            continue
    
        seen_in_feed = total = hits = 0
        for entry in d.entries:
            total += 1
            uid = entry.get("id") or entry.get("link")
            if not uid or uid in seen_ids:
                continue
            seen_ids.add(uid)
            seen_in_feed += 1
            matched = match(entry)
            if matched:
                hits += 1
                entry["_matched_keywords"] = matched
                entry["_published_dt"] = parse_published(entry)
                hits_by_feed[url].append(entry)
    
        elapsed = time.time() - t0
        stats.append((url, total, hits, f"{elapsed:.1f}s {status} {nbytes//1024}KB"))


    all_hits = round_robin_select(hits_by_feed, MAX_ITEMS)

    # ----- Build output feed -----
    fg = FeedGenerator()
    fg.id("feed-filter:filtered")
    fg.title("Filtered Research Feed")
    fg.link(href="https://example.com/filtered.xml", rel="self")
    fg.description(
        f"Aggregated from {len(CFG['feeds'])} sources, "
        f"filtered by {len(ALL_PATTERNS)} keywords. "
        f"{len(all_hits)} matching items."
    )
    fg.language("en")

    for entry in all_hits:
        fe = fg.add_entry()
        fe.id(entry.get("id") or entry.get("link"))
        title = entry.get("title", "(untitled)")
        kws = entry.get("_matched_keywords", [])
        # Prepend matched keywords to title for at-a-glance
        tag_str = " | ".join(kws[:3])
        fe.title(f"[{tag_str}] {title}" if tag_str else title)
        fe.link(href=entry.get("link", ""))
        # Include matched keywords + summary in description
        summary = entry.get("summary", "")
        kw_block = f"<p><b>Matched:</b> {', '.join(kws)}</p>" if kws else ""
        fe.description(kw_block + summary)
        fe.pubDate(entry["_published_dt"])
        for author in entry.get("authors", []):
            name = author.get("name") if isinstance(author, dict) else str(author)
            if name:
                fe.author({"name": name})

    out_dir = ROOT / "public"
    out_dir.mkdir(exist_ok=True)
    fg.rss_file(str(out_dir / "filtered.xml"))

    # ----- Print stats for Action log -----
    print("=" * 72)
    print(f"{'feed':<60} {'total':>6} {'hits':>6}")
    print("-" * 72)
    for url, total, hits, note in stats:
        short = url if len(url) < 58 else "..." + url[-55:]
        print(f"{short:<60} {total:>6} {hits:>6}  {note}")
    print("-" * 72)
    print(f"Wrote {len(all_hits)} items to public/filtered.xml")


if __name__ == "__main__":
    main()
