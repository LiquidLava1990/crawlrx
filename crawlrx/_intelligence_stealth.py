"""Stealth-profile selection helpers for ``intelligence.py``.

Hosts the SQL fan-out that the host-side ``get_best_stealth_profile`` used to
embed inline (5-deep nesting, cognitive 20). Each helper does one thing.
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional


def _exact_profile(
    conn: sqlite3.Connection,
    domain: str,
    *,
    min_attempts: int = 5,
    min_score: float = 0.5,
) -> Optional[str]:
    """Best stealth profile for an exact domain match."""
    row = conn.execute(
        """
        SELECT profile_hash, score FROM stealth_scores
        WHERE domain = ? AND attempts >= ?
        ORDER BY score DESC LIMIT 1
        """,
        (domain, min_attempts),
    ).fetchone()
    if row and row["score"] > min_score:
        return row["profile_hash"]
    return None


def _category_profile(
    conn: sqlite3.Connection,
    similar_domains: List[str],
    *,
    min_attempts: int = 3,
    min_score: float = 0.5,
) -> Optional[str]:
    """Best stealth profile averaged across a list of similar domains."""
    if not similar_domains:
        return None
    like_conditions = " OR ".join("domain LIKE ?" for _ in similar_domains)
    like_params = [f"%{d}%" for d in similar_domains]
    row = conn.execute(
        f"""
        SELECT profile_hash, AVG(score) as avg_score
        FROM stealth_scores
        WHERE ({like_conditions}) AND attempts >= ?
        GROUP BY profile_hash
        ORDER BY avg_score DESC LIMIT 1
        """,
        [*like_params, min_attempts],
    ).fetchone()
    if row and row["avg_score"] > min_score:
        return row["profile_hash"]
    return None


def best_stealth_profile(
    conn: sqlite3.Connection,
    domain: str,
    similar_domains: List[str],
) -> Optional[str]:
    """Top-level resolver: exact match first, similar-domain fallback second."""
    exact = _exact_profile(conn, domain)
    if exact:
        return exact
    return _category_profile(conn, similar_domains)
