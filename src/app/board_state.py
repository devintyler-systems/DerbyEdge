"""Deterministic, card-scoped Race Board state helpers.

This module deliberately has no Streamlit dependency so the run-selection and
live-odds rules can be exercised without rendering the application.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd


LIVE_ODDS_UNAVAILABLE = (
    "Live odds unavailable — showing score-run morning-line value estimates."
)


@dataclass(frozen=True)
class LiveOddsOverlay:
    board: pd.DataFrame
    available: bool
    message: str
    snapshot_timestamp: str | None = None
    snapshot_source: str | None = None


def load_run_index_for_card(conn, card_id: int) -> list[dict]:
    """Return score runs for exactly one card in stable newest-first order."""
    rows = conn.execute(
        """SELECT sr.run_id, sr.run_timestamp, sr.model_type,
                  mr.model_name, mr.version
           FROM score_runs sr
           LEFT JOIN model_registry mr ON sr.model_id = mr.model_id
           WHERE sr.card_id = ?
           ORDER BY sr.run_timestamp DESC, sr.run_id DESC""",
        (card_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def latest_run_id_for_card(conn, card_id: int) -> str | None:
    """Resolve the newest run using the same ordering as the selector."""
    runs = load_run_index_for_card(conn, card_id)
    return runs[0]["run_id"] if runs else None


def select_active_run_id(
    runs: Sequence[Mapping[str, object]], selected_run_id: str | None
) -> str | None:
    """Keep a valid manual pin, otherwise use the card-local newest run.

    The caller supplies only the active card's runs, so an ID from another card
    can never be returned.
    """
    run_ids = [str(run["run_id"]) for run in runs]
    if selected_run_id in run_ids:
        return selected_run_id
    return run_ids[0] if run_ids else None


def _unavailable(board: pd.DataFrame, reason: str) -> LiveOddsOverlay:
    out = board.copy()
    # Explicit unavailable columns prevent downstream renderers from treating a
    # partial snapshot as a market vector. Persisted board columns stay intact.
    out["live_decimal_odds"] = np.nan
    out["live_market_prob"] = np.nan
    return LiveOddsOverlay(out, False, LIVE_ODDS_UNAVAILABLE, None, reason)


def apply_live_odds_overlay(
    board: pd.DataFrame,
    live_by_pp: Mapping[int, Mapping[str, object]] | None,
    *,
    bet_edge_threshold: float,
    underlay_edge_threshold: float,
) -> LiveOddsOverlay:
    """Apply a complete, exactly-mapped live snapshot to a persisted board.

    A snapshot is usable only when every active entry has one valid decimal
    price and the snapshot entry_id agrees with the score row.  This avoids
    both guessed horse ordering and accidental mixing of live and morning-line
    probabilities.  When unusable, stored score-run edge and tags are returned
    without modification.
    """
    required = {"entry_id", "post_position", "win_probability"}
    if board.empty:
        return _unavailable(board, "empty board")
    if not required.issubset(board.columns):
        return _unavailable(board, "board is missing live-overlay keys")
    if not live_by_pp:
        return _unavailable(board, "no live odds snapshot")

    try:
        posts = [int(post) for post in board["post_position"]]
        entry_ids = [int(entry_id) for entry_id in board["entry_id"]]
    except (TypeError, ValueError):
        return _unavailable(board, "invalid board post or entry mapping")
    if len(set(posts)) != len(posts) or len(set(entry_ids)) != len(entry_ids):
        return _unavailable(board, "ambiguous board post or entry mapping")
    if set(live_by_pp) != set(posts):
        return _unavailable(board, "incomplete live odds snapshot")

    decimals: list[float] = []
    timestamps: set[str] = set()
    sources: set[str] = set()
    for post, entry_id in zip(posts, entry_ids):
        quote = live_by_pp.get(post)
        if not quote or quote.get("entry_id") is None:
            return _unavailable(board, "live odds snapshot lacks entry mapping")
        try:
            quote_entry_id = int(quote["entry_id"])
            decimal = float(quote["decimal_odds"])
        except (TypeError, ValueError):
            return _unavailable(board, "invalid live odds quote")
        if quote_entry_id != entry_id or not np.isfinite(decimal) or decimal <= 1.0:
            return _unavailable(board, "live odds entry mapping does not match board")
        decimals.append(decimal)
        if quote.get("captured_at"):
            timestamps.add(str(quote["captured_at"]))
        if quote.get("book_id"):
            sources.add(str(quote["book_id"]))

    model_prob = pd.to_numeric(board["win_probability"], errors="coerce")
    if model_prob.isna().any() or not np.isfinite(model_prob.to_numpy()).all():
        return _unavailable(board, "invalid persisted model probabilities")
    raw_market = 1.0 / np.asarray(decimals, dtype=float)
    normalized_market = raw_market / raw_market.sum()
    edge = model_prob.to_numpy(dtype=float) - normalized_market

    out = board.copy()
    # Retain an audit copy whenever a live overlay changes display values.
    for column in ("market_implied_prob", "value_score", "bet_tag"):
        if column in out:
            out[f"score_run_{column}"] = out[column]
    out["live_decimal_odds"] = decimals
    out["live_market_prob"] = normalized_market
    out["market_implied_prob"] = normalized_market
    out["value_score"] = edge
    tags = np.where(edge >= bet_edge_threshold, "bet",
                    np.where(edge < underlay_edge_threshold, "underlay", "neutral"))
    if "confidence_flag" in out:
        low_confidence = pd.to_numeric(out["confidence_flag"], errors="coerce").fillna(0) == 0
        tags = np.where(low_confidence.to_numpy(), "neutral", tags)
    out["bet_tag"] = tags
    timestamp = max(timestamps) if timestamps else None
    source = ", ".join(sorted(sources)) if sources else None
    return LiveOddsOverlay(out, True, "Live odds snapshot active.", timestamp, source)
