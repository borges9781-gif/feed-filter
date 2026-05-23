"""
common.py — shared logic for all fetchers (filter.py / openreview_fetch.py / acl_fetch.py).

Single source of truth for:
  * keyword loading + regex compilation (case-insensitive + case-sensitive lists)
  * matching an arbitrary text blob against the keyword set
  * round-robin selection across sources (so a high-volume source cannot
    crowd out a low-volume one — same philosophy as filter.py)

Keeping these here means: edit config.yaml once, and arXiv RSS, OpenReview,
and ACL Anthology all use the exact same keywords and the same fairness rule.
"""
import re
import yaml
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).parent


# ----------------------------------------------------------------------
# Keyword loading & compilation
# ----------------------------------------------------------------------
def load_main_config():
    """Load config.yaml (the shared keyword + field config)."""
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


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


def build_patterns(cfg):
    """Return the combined (kw, regex) list from a loaded config dict."""
    ci = compile_patterns(cfg.get("keywords", []), case_sensitive=False)
    cs = compile_patterns(cfg.get("case_sensitive_keywords", []), case_sensitive=True)
    return ci + cs


def match_text(text, patterns):
    """Return list of matched keywords for a text blob, or [] if none."""
    return [kw for kw, pat in patterns if pat.search(text)]


# ----------------------------------------------------------------------
# Round-robin selection (the "fair" selection method from filter.py)
# ----------------------------------------------------------------------
def round_robin_select(hits_by_source, max_items, sort_key="_published_dt"):
    """Round-robin selection: take latest from each source alternately.

    hits_by_source : dict {source_name: [item, ...]}
                     each item must be a dict carrying `sort_key`
    max_items      : global cap on returned items
    sort_key       : item field used to order WITHIN a source (desc)

    Guarantees:
      - every source gets fair representation (no single source dominates)
      - within each source, newest / most-relevant items come first
      - small sources still contribute even if a big source has hundreds
    """
    sources_with_hits = [(name, hits) for name, hits in hits_by_source.items() if hits]
    if not sources_with_hits:
        return []

    for _name, hits in sources_with_hits:
        hits.sort(key=lambda e: e.get(sort_key) or 0, reverse=True)

    result = []
    indices = {name: 0 for name, _ in sources_with_hits}

    while len(result) < max_items:
        made_progress = False
        for name, _ in sources_with_hits:
            if len(result) >= max_items:
                break
            idx = indices[name]
            if idx < len(hits_by_source[name]):
                result.append(hits_by_source[name][idx])
                indices[name] += 1
                made_progress = True
        if not made_progress:
            break

    return result


# ----------------------------------------------------------------------
# Misc helpers
# ----------------------------------------------------------------------
def ms_to_dt(ms):
    """OpenReview cdate is epoch milliseconds -> aware UTC datetime."""
    if not ms:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)
