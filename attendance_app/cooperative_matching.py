"""
Match free-text cooperative names to cooperative_registry.official_name.
When multiple registry names have similar scores, prefer the row with the highest id
(newest row in the registry table).
"""
from difflib import SequenceMatcher
from typing import Any, List, Optional, Union

import sqlite3


def normalize_coop_name(s: str) -> str:
    if not s:
        return ""
    t = s.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return " ".join(t.split()).strip().lower()


_STOP = frozenset(
    {
        "multi",
        "purpose",
        "cooperative",
        "co-operative",
        "co",
        "operative",
        "society",
        "limited",
        "the",
        "and",
        "in",
        "for",
        "of",
        "to",
        "a",
        "an",
        "by",
        "small",
        "scale",
        "mining",
    }
)


def _effective_match_ratio(raw_n: str, off_n: str) -> float:
    """
    Combine SequenceMatcher with a light token check so short labels like
    'Capricon MPCs' can match long official names that contain the same distinctive tokens.
    """
    base = SequenceMatcher(None, raw_n, off_n).ratio()
    if not raw_n or not off_n:
        return base
    toks = [w for w in raw_n.replace("-", " ").split() if len(w) >= 3]
    significant = [w for w in toks if w not in _STOP]
    if not significant:
        significant = toks
    if not significant:
        return base
    hits = sum(1 for w in significant if w in off_n)
    need = max(1, (len(significant) + 1) // 2)
    if hits >= need and len(raw_n) < len(off_n) * 0.6:
        return max(base, 0.82)
    return base


def resolve_cooperative_to_registry(
    raw: str,
    registry_rows: List[Union[sqlite3.Row, Any]],  # Row-like with id, official_name
    similarity_threshold: float = 0.78,
    ambiguity_band: float = 0.03,
) -> Optional[str]:
    """
    Return official_name from registry, or None if no row meets the threshold.
    `registry_rows`: rows with at least `id` and `official_name`.
    """
    # Late import avoids cycles when db imports this module inside remap only.
    raw_n = normalize_coop_name(raw)
    if not raw_n:
        return None

    norm_to_official: dict = {}
    for row in registry_rows:
        off = str(row["official_name"])
        key = normalize_coop_name(off)
        if not key:
            continue
        rid = int(row["id"])
        if key not in norm_to_official or rid > norm_to_official[key][1]:
            norm_to_official[key] = (off, rid)

    if raw_n in norm_to_official:
        return norm_to_official[raw_n][0]

    scored: List[tuple] = []
    for row in registry_rows:
        off = str(row["official_name"])
        off_n = normalize_coop_name(off)
        if not off_n:
            continue
        ratio = _effective_match_ratio(raw_n, off_n)
        scored.append((row, ratio))

    if not scored:
        return None

    scored.sort(key=lambda x: (-x[1], -int(x[0]["id"])))
    best_ratio = scored[0][1]
    if best_ratio < similarity_threshold:
        return None

    close = [
        (row, r)
        for row, r in scored
        if r >= best_ratio - ambiguity_band and r >= similarity_threshold
    ]
    if not close:
        return str(scored[0][0]["official_name"])

    row_pick, _ = max(close, key=lambda x: (x[1], int(x[0]["id"])))
    return str(row_pick["official_name"])


def resolve_cooperative_for_storage(
    raw: str,
    db_path: str,
    similarity_threshold: float = 0.78,
    ambiguity_band: float = 0.03,
) -> str:
    """If registry is loaded, map raw name to official; else return stripped raw."""
    import db as dbmod

    if dbmod.cooperative_registry_count(db_path) == 0:
        return (raw or "").strip()
    rows = dbmod.get_cooperative_registry_rows(db_path)
    resolved = resolve_cooperative_to_registry(
        raw,
        rows,
        similarity_threshold=similarity_threshold,
        ambiguity_band=ambiguity_band,
    )
    if resolved:
        return resolved
    return (raw or "").strip()
