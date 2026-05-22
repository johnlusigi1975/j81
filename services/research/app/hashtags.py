"""Hashtag-aware query expansion.

Search engines and yt-dlp generally strip the leading '#', so for each
hashtag we search both the bare term and the literal hashtag, and bias the
query toward Deriv so we don't pull unrelated noise.
"""

from __future__ import annotations

# Sensible Deriv defaults if a topic supplies none.
DEFAULT_DERIV_HASHTAGS = [
    "#deriv",
    "#derivtrading",
    "#derivbot",
    "#binarycom",
    "#syntheticindices",
    "#volatilityindex",
]


def normalize_hashtag(tag: str) -> str:
    return "#" + tag.lstrip("#").strip().lower()


def build_query(base_query: str, hashtags: list[str]) -> str:
    """Combine a base query with hashtag terms into one search string.

    Example: build_query("even odd strategy", ["#deriv", "#derivbot"])
      -> 'even odd strategy deriv (#deriv OR #derivbot OR deriv OR derivbot)'
    """
    base = base_query.strip()
    if not hashtags:
        return base

    tags = [normalize_hashtag(t) for t in hashtags]
    bare = [t.lstrip("#") for t in tags]
    alternatives = " OR ".join(dict.fromkeys(tags + bare))
    # Always anchor on "deriv" so hashtag chasing stays on-topic.
    anchor = "" if "deriv" in base.lower() else "deriv "
    return f"{base} {anchor}({alternatives})".strip()
