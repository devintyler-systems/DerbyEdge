"""Runners with no past performances (first-time starters, or DraftKings printed "No details available.").

Their history-based features are empty, so the stored forecast for them rests on defaults, not on a record.  The board labels them and the
pre-threshold picks carry a warning; nothing here changes the scoring.  The signal is ``feature_store.dk_history_start_count == 0``
(past performances stored for the horse before the race).
"""
from __future__ import annotations

NO_HISTORY_WARNING = "NO PP HISTORY: forecast rests on defaults"
NO_HISTORY_NOTE = ("No past performances (first-time starters, or DraftKings had no details): their Diagnostic Win % rests on "
                   "defaults, not on a record, so treat it as no information about the horse")


def no_history_entry_ids(feature_frame) -> set[int]:
    """Entry ids whose stored history count is exactly zero (a missing count is not evidence of no history)."""
    if feature_frame is None or len(feature_frame) == 0:
        return set()
    if "dk_history_start_count" not in feature_frame.columns or "entry_id" not in feature_frame.columns:
        return set()
    import pandas as pd
    counts = pd.to_numeric(feature_frame["dk_history_start_count"], errors="coerce")
    return {int(e) for e, c in zip(feature_frame["entry_id"], counts) if pd.notna(c) and c == 0 and pd.notna(e)}
