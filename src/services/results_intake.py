"""Post-race results intake service for Operator Console.

Parses an official result CSV/TSV, resolves entries via DB lookup,
writes to race_results, and evaluates a prior score_run.

Required CSV columns:
    race_date, track_code, race_number, horse_name, finish_position

Optional CSV columns:
    official_odds, post_position, beaten_lengths, scratched, disqualified,
    speed_figure, beyer_figure, final_time, earned_purse, comment

Column aliases (any of these names are accepted):
    race_date       : race_date | date | race_dt
    track_code      : track_code | track | trk | track_abbrev
    race_number     : race_number | race_num | race | rn
    horse_name      : horse_name | horse | name | entry
    finish_position : finish_position | finish | fin | pos | position | place
    official_odds   : official_odds | odds | mutuels | final_odds | win_odds
    post_position   : post_position | post | pp | pgm
    beaten_lengths  : beaten_lengths | lengths_behind | beaten | lb | blen
    scratched       : scratched | scratch | scr | is_scratched
    disqualified    : disqualified | dq | dqed | is_dq | is_disqualified
    speed_figure    : speed_figure | speed_fig | fig | spd_fig
    beyer_figure    : beyer_figure | beyer | bf | beyer_fig
    final_time      : final_time | time | finish_time | winning_time
    earned_purse    : earned_purse | earned | purse | earnings
    comment         : comment | notes | note | trip

Training label notes
--------------------
race_results.official_finish is the ground-truth label for win prediction.
To produce a labeled training example for a completed race:

    SELECT
        es.entry_id,
        es.win_probability          AS pred_win_prob,
        es.value_score              AS pred_edge,
        es.bet_tag,
        rr.official_finish          AS actual_finish,
        CASE WHEN rr.official_finish = 1 THEN 1 ELSE 0 END AS won,
        rr.official_odds_decimal    AS actual_odds,
        rr.is_scratched,
        rr.is_disqualified
    FROM entry_scores es
    JOIN race_results rr ON es.entry_id = rr.entry_id
    WHERE es.run_id = ?
    ORDER BY es.rank;

Scratched and DQ horses should be excluded from win-probability calibration
(is_scratched=1 or official_finish IS NULL).
"""
from __future__ import annotations

import csv
import difflib
import io
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.derbyedge.tracks import resolve_track as _resolve_track

# ── Column alias sets ─────────────────────────────────────────────────────────
RACE_DATE_ALIASES  = {"race_date", "date", "race_dt"}
TRACK_CODE_ALIASES = {"track_code", "track", "trk", "track_abbrev"}
RACE_NUM_ALIASES   = {"race_number", "race_num", "race", "rn"}
HORSE_ALIASES      = {"horse_name", "horse", "name", "entry"}
FINISH_ALIASES     = {"finish_position", "finish", "fin", "pos", "position", "place"}
ODDS_ALIASES       = {"official_odds", "odds", "mutuels", "final_odds", "win_odds"}
POST_ALIASES       = {"post_position", "post", "pp", "pgm"}
BEATEN_ALIASES     = {"beaten_lengths", "lengths_behind", "beaten", "lb", "blen"}
SCRATCH_ALIASES    = {"scratched", "scratch", "scr", "is_scratched"}
DQ_ALIASES         = {"disqualified", "dq", "dqed", "is_dq", "is_disqualified"}
SPEED_ALIASES      = {"speed_figure", "speed_fig", "fig", "spd_fig"}
BEYER_ALIASES      = {"beyer_figure", "beyer", "bf", "beyer_fig"}
TIME_ALIASES       = {"final_time", "time", "finish_time", "winning_time"}
EARNED_ALIASES     = {"earned_purse", "earned", "purse", "earnings"}
COMMENT_ALIASES    = {"comment", "notes", "note", "trip"}

ParseResult = dict[str, Any]
ParseError  = dict[str, Any]

# ── Table DDL (auto-created on first ingest if missing) ───────────────────────
_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS race_results (
    result_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id                 INTEGER NOT NULL REFERENCES race_cards(card_id),
    entry_id                INTEGER          REFERENCES entries(entry_id),
    horse_id                INTEGER NOT NULL REFERENCES horses(horse_id),
    post_position           INTEGER,
    finish_position         INTEGER,
    official_finish         INTEGER,
    is_scratched            INTEGER NOT NULL DEFAULT 0 CHECK(is_scratched IN (0,1)),
    is_disqualified         INTEGER NOT NULL DEFAULT 0 CHECK(is_disqualified IN (0,1)),
    official_odds_decimal   REAL,
    official_odds_american  INTEGER,
    beaten_lengths          REAL,
    speed_figure            INTEGER,
    beyer_figure            INTEGER,
    final_time              TEXT,
    earned_purse            INTEGER,
    comment                 TEXT,
    ingested_at             TEXT NOT NULL,
    UNIQUE(card_id, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_rr_card  ON race_results(card_id);
CREATE INDEX IF NOT EXISTS idx_rr_horse ON race_results(horse_id);
CREATE INDEX IF NOT EXISTS idx_rr_entry ON race_results(entry_id);
"""


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_RESULTS_DDL)
    conn.commit()


# ── Internal parsers ──────────────────────────────────────────────────────────
def _find_col(fieldnames: list[str], aliases: set[str]) -> str | None:
    for col in fieldnames:
        if col.strip().lower() in aliases:
            return col
    return None


def _parse_date(s: str) -> str | None:
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d-%b-%Y",
                "%d-%b-%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _parse_bool(s: str) -> bool:
    return str(s).strip().lower() in {"1", "true", "yes", "y", "x", "scratched", "scr", "dq"}


def _parse_int(s: str) -> int | None:
    if not s:
        return None
    try:
        return int(str(s).strip().split(".")[0])
    except ValueError:
        return None


def _parse_float(s: str) -> float | None:
    if not s:
        return None
    cleaned = re.sub(r"[$,]", "", str(s).strip())
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_odds(raw: str) -> float | None:
    """Accept decimal (e.g. '3.40'), fractional ('9/2'), or American ('+450')."""
    raw = raw.strip()
    if not raw:
        return None
    # fractional "9/2"
    m = re.match(r"^(\d+)/(\d+)$", raw)
    if m:
        return round(int(m.group(1)) / int(m.group(2)) + 1.0, 3)
    # american "+450" or "-110"
    m = re.match(r"^([+-])(\d+)$", raw)
    if m:
        val = int(m.group(2)) * (1 if m.group(1) == "+" else -1)
        if val > 0:
            return round(val / 100 + 1.0, 3)
        elif val < 0:
            return round(-100 / val + 1.0, 3)
    v = _parse_float(raw)
    if v is not None and v >= 1.0:
        return v
    return None


def _norm_name(s: str) -> str:
    return " ".join(s.strip().split()).title() if s else ""


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Public parsers ────────────────────────────────────────────────────────────
def parse_results_csv(raw: bytes | str) -> tuple[list[ParseResult], list[ParseError]]:
    """Parse official result CSV/TSV into normalized rows + per-row errors.

    Auto-detects TSV vs CSV from header row.
    Returns (parsed_rows, error_rows).
    """
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw

    first = text.split("\n")[0] if text else ""
    sep = "\t" if "," not in first and "\t" in first else ","

    reader = csv.DictReader(io.StringIO(text), delimiter=sep)
    fnames = reader.fieldnames or []
    if not fnames:
        return [], [{"row": 0, "raw": {}, "reason": "No headers found in file"}]

    c_date    = _find_col(fnames, RACE_DATE_ALIASES)
    c_track   = _find_col(fnames, TRACK_CODE_ALIASES)
    c_racenum = _find_col(fnames, RACE_NUM_ALIASES)
    c_horse   = _find_col(fnames, HORSE_ALIASES)
    c_finish  = _find_col(fnames, FINISH_ALIASES)
    c_odds    = _find_col(fnames, ODDS_ALIASES)
    c_post    = _find_col(fnames, POST_ALIASES)
    c_beaten  = _find_col(fnames, BEATEN_ALIASES)
    c_scratch = _find_col(fnames, SCRATCH_ALIASES)
    c_dq      = _find_col(fnames, DQ_ALIASES)
    c_speed   = _find_col(fnames, SPEED_ALIASES)
    c_beyer   = _find_col(fnames, BEYER_ALIASES)
    c_time    = _find_col(fnames, TIME_ALIASES)
    c_earned  = _find_col(fnames, EARNED_ALIASES)
    c_comment = _find_col(fnames, COMMENT_ALIASES)

    missing = []
    if not c_date:    missing.append("race_date")
    if not c_track:   missing.append("track_code")
    if not c_racenum: missing.append("race_number")
    if not c_horse:   missing.append("horse_name")
    if missing:
        return [], [{
            "row": 0, "raw": {},
            "reason": (f"Required columns not found: {', '.join(missing)}. "
                       f"Headers detected: {', '.join(fnames)}"),
        }]

    parsed: list[ParseResult] = []
    errors: list[ParseError]  = []

    for row_n, row in enumerate(reader, start=2):
        raw_dict = dict(row)

        horse = _norm_name(row.get(c_horse, ""))
        if not horse:
            errors.append({"row": row_n, "raw": raw_dict, "reason": "Blank horse_name"})
            continue

        race_date = _parse_date(row.get(c_date, ""))
        if not race_date:
            errors.append({"row": row_n, "raw": raw_dict,
                           "reason": f"Unparseable date: {row.get(c_date, '')!r}"})
            continue

        track_code = (row.get(c_track) or "").strip().upper()
        if not track_code:
            errors.append({"row": row_n, "raw": raw_dict, "reason": "Blank track_code"})
            continue

        race_num = _parse_int(row.get(c_racenum, ""))
        if race_num is None:
            errors.append({"row": row_n, "raw": raw_dict,
                           "reason": f"Unparseable race_number: {row.get(c_racenum, '')!r}"})
            continue

        is_scratched = _parse_bool(row.get(c_scratch, "")) if c_scratch else False
        is_dq        = _parse_bool(row.get(c_dq, "")) if c_dq else False

        # finish_position is blank/null for scratches
        finish = None
        if not is_scratched and c_finish:
            finish = _parse_int(row.get(c_finish, ""))

        parsed.append({
            "race_date":            race_date,
            "track_code":           track_code,
            "race_number":          race_num,
            "horse_name":           horse,
            "finish_position":      finish,
            "is_scratched":         is_scratched,
            "is_disqualified":      is_dq,
            "official_odds_decimal":_parse_odds(row.get(c_odds, "") or "") if c_odds else None,
            "post_position":        _parse_int(row.get(c_post, "")) if c_post else None,
            "beaten_lengths":       _parse_float(row.get(c_beaten, "")) if c_beaten else None,
            "speed_figure":         _parse_int(row.get(c_speed, "")) if c_speed else None,
            "beyer_figure":         _parse_int(row.get(c_beyer, "")) if c_beyer else None,
            "final_time":           (row.get(c_time) or "").strip() or None if c_time else None,
            "earned_purse":         _parse_int(row.get(c_earned, "")) if c_earned else None,
            "comment":              (row.get(c_comment) or "").strip() or None if c_comment else None,
        })

    return parsed, errors


# ── Row normalization ─────────────────────────────────────────────────────────

def _normalize_result_row(row: dict) -> dict:
    """Return row with all optional fields populated from safe defaults.

    Allows ingest_results and preview_results_match to accept rows from both
    parse_results_csv and parse_results_pdf without KeyError on absent fields.

    Required keys (race_date, track_code, race_number, horse_name) use direct
    indexing so callers still get an informative KeyError when truly missing.

    Normalized schema
    -----------------
    race_date             str          YYYY-MM-DD
    track_code            str          e.g. "PEN"
    race_number           int
    horse_name            str
    finish_position       int | None
    is_scratched          bool         default False
    is_disqualified       bool         default False
    coupled_entry         str | None   default None
    official_odds_decimal float | None default None
    post_position         int | None   default None
    beaten_lengths        float | None default None
    speed_figure          int | None   default None
    beyer_figure          int | None   default None
    final_time            str | None   default None
    earned_purse          int | None   default None
    comment               str | None   default None
    """
    return {
        # Required
        "race_date":             row["race_date"],
        "track_code":            row["track_code"],
        "race_number":           row["race_number"],
        "horse_name":            row["horse_name"],
        # Boolean flags
        "is_scratched":          bool(row.get("is_scratched",    False)),
        "is_disqualified":       bool(row.get("is_disqualified", False)),
        "coupled_entry":         row.get("coupled_entry"),
        # Finish / odds / figures
        "finish_position":       row.get("finish_position"),
        "official_odds_decimal": row.get("official_odds_decimal"),
        "post_position":         row.get("post_position"),
        "beaten_lengths":        row.get("beaten_lengths"),
        "speed_figure":          row.get("speed_figure"),
        "beyer_figure":          row.get("beyer_figure"),
        "final_time":            row.get("final_time"),
        "earned_purse":          row.get("earned_purse"),
        "comment":               row.get("comment"),
    }


# ── DB lookup helpers ─────────────────────────────────────────────────────────
def _resolve_track_code(raw: str) -> str:
    """Return canonical track code for raw; falls back to raw uppercased if unresolved."""
    res = _resolve_track(track_code=raw, track_name=raw)
    return res["track_code"] or raw.strip().upper()


def _find_card_id(
    conn: sqlite3.Connection,
    track_code: str,
    race_date: str,
    race_number: int,
) -> int | None:
    row = conn.execute(
        """SELECT rc.card_id FROM race_cards rc
           JOIN tracks t ON rc.track_id = t.track_id
           WHERE t.abbrev = ? AND rc.card_date = ? AND rc.race_number = ?""",
        (track_code, race_date, race_number),
    ).fetchone()
    return row[0] if row else None


def _find_entry(
    conn: sqlite3.Connection,
    card_id: int,
    horse_name: str,
    threshold: float = 0.72,
) -> tuple[int | None, str | None, float]:
    """Return (entry_id, matched_name, score). Exact first, then fuzzy."""
    rows = conn.execute(
        """SELECT e.entry_id, h.name FROM entries e
           JOIN horses h ON e.horse_id = h.horse_id
           WHERE e.card_id = ?""",
        (card_id,),
    ).fetchall()
    if not rows:
        return None, None, 0.0

    name_map = {r[1].lower(): (r[0], r[1]) for r in rows}
    h_lower = horse_name.lower()
    if h_lower in name_map:
        eid, mname = name_map[h_lower]
        return eid, mname, 1.0

    candidates = difflib.get_close_matches(
        h_lower, list(name_map.keys()), n=1, cutoff=threshold
    )
    if candidates:
        eid, mname = name_map[candidates[0]]
        score = difflib.SequenceMatcher(None, h_lower, candidates[0]).ratio()
        return eid, mname, score
    return None, None, 0.0


# ── Preview ───────────────────────────────────────────────────────────────────
def preview_results_match(
    conn: sqlite3.Connection,
    parsed_rows: list[ParseResult],
    threshold: float = 0.72,
) -> dict:
    """Dry-run: resolved races, matched/unmatched/duplicate horses — no inserts."""
    _ensure_table(conn)

    card_cache: dict[tuple, int | None] = {}
    races_found:   list[dict] = []
    races_missing: list[dict] = []
    seen_race_keys: set[tuple] = set()

    horses_matched:   list[dict] = []
    horses_unmatched: list[dict] = []
    horses_duplicate: list[dict] = []

    for row in parsed_rows:
        row = _normalize_result_row(row)
        resolved_tc = _resolve_track_code(row["track_code"])
        key = (resolved_tc, row["race_date"], row["race_number"])

        if key not in card_cache:
            cid = _find_card_id(conn, *key)
            card_cache[key] = cid
            if key not in seen_race_keys:
                seen_race_keys.add(key)
                if cid:
                    races_found.append({
                        "track_code":  resolved_tc,
                        "race_date":   row["race_date"],
                        "race_number": row["race_number"],
                        "card_id":     cid,
                    })
                else:
                    races_missing.append({
                        "track_code":  resolved_tc,
                        "race_date":   row["race_date"],
                        "race_number": row["race_number"],
                    })

        card_id = card_cache[key]
        if card_id is None:
            continue

        entry_id, matched_name, score = _find_entry(conn, card_id, row["horse_name"], threshold)
        if entry_id is None:
            horses_unmatched.append({
                "horse_name":  row["horse_name"],
                "track_code":  row["track_code"],
                "race_date":   row["race_date"],
                "race_number": row["race_number"],
            })
            continue

        dup = conn.execute(
            "SELECT COUNT(*) FROM race_results WHERE card_id=? AND entry_id=?",
            (card_id, entry_id),
        ).fetchone()[0] > 0

        entry = {
            "horse_name":   row["horse_name"],
            "matched_name": matched_name,
            "match_score":  round(score, 3),
            "track_code":   row["track_code"],
            "race_date":    row["race_date"],
            "race_number":  row["race_number"],
            "finish":       row["finish_position"],
            "scratched":    row["is_scratched"],
            "card_id":      card_id,
        }
        (horses_duplicate if dup else horses_matched).append(entry)

    return {
        "races_found":      races_found,
        "races_missing":    races_missing,
        "horses_matched":   horses_matched,
        "horses_unmatched": horses_unmatched,
        "horses_duplicate": horses_duplicate,
    }


# ── Ingest ────────────────────────────────────────────────────────────────────
def ingest_results(
    conn: sqlite3.Connection,
    parsed_rows: list[ParseResult],
    threshold: float = 0.72,
) -> dict:
    """Insert parsed result rows into race_results.

    Uses INSERT OR IGNORE on (card_id, entry_id) so re-uploads are idempotent.
    Confirmed scratches update entries.scratch_flag = 1.
    Returns summary dict.
    """
    _ensure_table(conn)
    ingested_at = _now_utc()

    n_inserted = n_unmatched_race = n_unmatched_horse = n_duplicate = n_scratch_flag = 0
    warnings: list[str] = []
    card_cache: dict[tuple, int | None] = {}

    for row in parsed_rows:
        row = _normalize_result_row(row)
        resolved_tc = _resolve_track_code(row["track_code"])
        key = (resolved_tc, row["race_date"], row["race_number"])
        if key not in card_cache:
            card_cache[key] = _find_card_id(conn, *key)
        card_id = card_cache[key]

        if card_id is None:
            n_unmatched_race += 1
            warnings.append(
                f"Race not in DB: {resolved_tc} R{row['race_number']} {row['race_date']}"
                + (f" (raw track_code={row['track_code']!r})" if resolved_tc != row["track_code"] else "")
            )
            continue

        entry_id, matched_name, score = _find_entry(conn, card_id, row["horse_name"], threshold)
        if entry_id is None:
            n_unmatched_horse += 1
            warnings.append(
                f"No entry match for '{row['horse_name']}' — "
                f"{row['track_code']} R{row['race_number']}"
            )
            continue

        dup = conn.execute(
            "SELECT COUNT(*) FROM race_results WHERE card_id=? AND entry_id=?",
            (card_id, entry_id),
        ).fetchone()[0]
        if dup:
            n_duplicate += 1
            continue

        horse_id = conn.execute(
            "SELECT horse_id FROM entries WHERE entry_id=?", (entry_id,)
        ).fetchone()[0]

        # official_finish = finish_position; cleared for DQ horses
        official_finish = row["finish_position"] if not row["is_disqualified"] else None

        # Derive american odds from decimal
        am_odds: int | None = None
        dec = row["official_odds_decimal"]
        if dec is not None and dec > 1.0:
            am_odds = (
                int(round((dec - 1.0) * 100)) if dec >= 2.0
                else int(round(-100.0 / (dec - 1.0)))
            )

        conn.execute(
            """INSERT OR IGNORE INTO race_results
               (card_id, entry_id, horse_id, post_position,
                finish_position, official_finish,
                is_scratched, is_disqualified,
                official_odds_decimal, official_odds_american,
                beaten_lengths, speed_figure, beyer_figure,
                final_time, earned_purse, comment, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                card_id, entry_id, horse_id, row["post_position"],
                row["finish_position"], official_finish,
                int(row["is_scratched"]), int(row["is_disqualified"]),
                row["official_odds_decimal"], am_odds,
                row["beaten_lengths"], row["speed_figure"], row["beyer_figure"],
                row["final_time"], row["earned_purse"], row["comment"],
                ingested_at,
            ),
        )
        n_inserted += 1

        if row["is_scratched"]:
            conn.execute(
                "UPDATE entries SET scratch_flag=1 WHERE entry_id=?", (entry_id,)
            )
            n_scratch_flag += 1

        if score < 1.0:
            warnings.append(
                f"Fuzzy '{row['horse_name']}' → '{matched_name}' (score {score:.2f})"
            )

    conn.commit()

    # Back-fill: entries with no race_results row are pre-ingest scratches — but only
    # when at least one non-scratched result was ingested for that race (meaning the full
    # field was processed and the absent horse was truly scratched, not just un-ingested).
    processed_card_ids = {cid for cid in card_cache.values() if cid is not None}
    for cid in processed_card_ids:
        conn.execute(
            """UPDATE entries SET scratch_flag = 1
               WHERE card_id = ?
                 AND scratch_flag = 0
                 AND EXISTS (
                     SELECT 1 FROM race_results rr
                     WHERE rr.card_id = ? AND rr.is_scratched = 0
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM race_results rr
                     WHERE rr.entry_id = entries.entry_id
                       AND rr.card_id = ?
                 )""",
            (cid, cid, cid),
        )
    if processed_card_ids:
        conn.commit()

    return {
        "n_inserted":        n_inserted,
        "n_unmatched_race":  n_unmatched_race,
        "n_unmatched_horse": n_unmatched_horse,
        "n_duplicate":       n_duplicate,
        "n_scratch_flag":    n_scratch_flag,
        "warnings":          warnings,
    }


# ── Scratch-aware helpers ─────────────────────────────────────────────────────

def get_effective_top_pick(
    conn: sqlite3.Connection, run_id: str, card_id: int
) -> dict | None:
    """Return the first non-scratched entry by model rank for a score run.

    Useful for any caller that needs the effective TP without running the full
    evaluate_score_run pipeline.  Returns None when all entries are scratched
    or no entry_scores exist for the run.
    """
    rows = conn.execute(
        """SELECT es.entry_id, es.horse_name, es.rank, es.win_probability,
                  COALESCE(rr.is_scratched, e.scratch_flag, 0) AS is_scratched
           FROM entry_scores es
           JOIN  entries e ON e.entry_id = es.entry_id
           LEFT JOIN race_results rr
                  ON rr.entry_id = es.entry_id AND rr.card_id = ?
           WHERE es.run_id = ?
           ORDER BY es.rank""",
        (card_id, run_id),
    ).fetchall()

    for row in rows:
        if not row[4]:  # is_scratched == 0
            return {
                "entry_id":        row[0],
                "horse_name":      row[1],
                "rank":            row[2],
                "win_probability": row[3],
            }
    return None


# ── Model evaluation ──────────────────────────────────────────────────────────
def evaluate_score_run(
    conn: sqlite3.Connection, run_id: str, card_id: int
) -> dict | None:
    """Compare a score_run's predictions against ingested race_results.

    Returns None if race_results table is missing or empty for this card.
    Returns a metrics dict otherwise — the caller decides how to display it.

    Kelly ROI uses a normalized $1,000 bankroll with 5% cap so the result
    is comparable across different user bankroll settings.
    """
    try:
        n_results = conn.execute(
            "SELECT COUNT(*) FROM race_results WHERE card_id=?", (card_id,)
        ).fetchone()[0]
        if n_results == 0:
            return None
    except Exception:
        return None

    # Fetch prediction + result rows; check entries.scratch_flag for pre-ingest scratches
    rows = conn.execute(
        """SELECT
               es.rank,
               es.horse_name,
               es.post_position,
               es.win_probability,
               es.bet_tag,
               es.value_score,
               es.morning_line_odds,
               es.market_implied_prob,
               rr.finish_position,
               rr.official_finish,
               COALESCE(rr.is_scratched, e.scratch_flag, 0) AS is_scratched,
               rr.is_disqualified,
               rr.official_odds_decimal,
               rr.beaten_lengths
           FROM entry_scores es
           JOIN  entries e ON e.entry_id = es.entry_id
           LEFT JOIN race_results rr ON es.entry_id = rr.entry_id
                                     AND rr.card_id = ?
           WHERE es.run_id = ?
           ORDER BY es.rank""",
        (card_id, run_id),
    ).fetchall()

    if not rows:
        return None

    COLS = [
        "rank", "horse_name", "post_position", "win_probability", "bet_tag",
        "value_score", "morning_line_odds", "market_implied_prob",
        "finish_position", "official_finish", "is_scratched", "is_disqualified",
        "official_odds_decimal", "beaten_lengths",
    ]
    data = [dict(zip(COLS, r)) for r in rows]

    # Fetch latest live_odds snapshot for Kelly calculation
    live_odds_map: dict[int, float] = {}
    for _q in [
        ("SELECT entry_id, decimal_odds FROM live_odds "
         "WHERE card_id=? AND is_morning_line=0 "
         "AND captured_at=(SELECT MAX(captured_at) FROM live_odds "
         "WHERE card_id=? AND is_morning_line=0)"),
        ("SELECT entry_id, decimal_odds FROM live_odds "
         "WHERE card_id=? "
         "AND captured_at=(SELECT MAX(captured_at) FROM live_odds WHERE card_id=?)"),
    ]:
        try:
            lo_rows = conn.execute(_q, (card_id, card_id)).fetchall()
            live_odds_map = {r[0]: float(r[1]) for r in lo_rows if r[1]}
            break
        except Exception:
            continue

    # Entry-id map for live odds lookup
    entry_ids = conn.execute(
        "SELECT entry_id, post_position FROM entries WHERE card_id=?", (card_id,)
    ).fetchall()
    pp_to_entry = {r[1]: r[0] for r in entry_ids}

    # ── Metrics ───────────────────────────────────────────────────────────────
    active = [r for r in data if not r["is_scratched"]]

    winner_row = next(
        (r for r in active if r["official_finish"] == 1 and not r["is_disqualified"]),
        next((r for r in active if r["finish_position"] == 1), None),
    )

    top = data[0]  # rank=1; may be scratched if horse scratched after scoring
    original_tp_scratched = bool(top.get("is_scratched"))

    # Effective top pick: original TP when not scratched; otherwise first
    # non-scratched entry by model rank.  Returns None only if all runners
    # in the score run are confirmed scratched (degenerate edge case).
    eff_top = top if not original_tp_scratched else next(
        (r for r in data if not r.get("is_scratched")),
        None,
    )
    effective_tp_won = bool(
        eff_top and winner_row and winner_row["horse_name"] == eff_top["horse_name"]
    )
    effective_tp_finish = eff_top.get("finish_position") if eff_top else None
    effective_tp_rank   = eff_top.get("rank")            if eff_top else None

    # Accuracy stats computed off effective TP so a scratched original pick
    # doesn't count as a model miss.
    top_pick_won    = effective_tp_won
    top_pick_finish = effective_tp_finish if not original_tp_scratched else None

    # ML favorite: lowest morning-line odds among non-confirmed-scratches.
    # is_scratched=NULL (horse not in race_results) is treated as not-confirmed-scratch;
    # confirmed scratches (is_scratched=1 from race_results) are excluded.
    ml_pool = [r for r in data if not r.get("is_scratched") and r.get("morning_line_odds") is not None]
    ml_fav  = min(ml_pool, key=lambda r: r["morning_line_odds"]) if ml_pool else None

    # Post-time favorite: lowest official_odds_decimal among horses that actually started.
    # official_odds_decimal is NULL for any horse not in race_results (scratches, etc.),
    # so the odds filter alone is sufficient to exclude scratches.
    ptf_pool = [r for r in data if not r.get("is_scratched") and r.get("official_odds_decimal") is not None]
    ptf_fav  = min(ptf_pool, key=lambda r: r["official_odds_decimal"]) if ptf_pool else None
    ptf_won  = bool(ptf_fav and winner_row and winner_row["horse_name"] == ptf_fav["horse_name"])

    # Top-3 hit rate
    top3_model  = {r["horse_name"] for r in data[:3]}
    actual_top3 = {
        r["horse_name"] for r in data
        if r["finish_position"] and r["finish_position"] <= 3 and not r["is_disqualified"]
    }
    top3_hit = len(top3_model & actual_top3)

    # BET-tagged performance
    bet_rows = [r for r in active if r["bet_tag"] == "bet"]
    bet_won  = [r for r in bet_rows if r["official_finish"] == 1]
    bet_itm  = [r for r in bet_rows
                if r["finish_position"] and r["finish_position"] <= 3
                and not r["is_disqualified"]]

    # Kelly ROI (normalized $1,000 bankroll, 5% cap)
    BANKROLL = 1_000.0
    KELLY_CAP = 0.05
    kelly_stake_total = 0.0
    kelly_return_total = 0.0

    for r in bet_rows:
        pp = r["post_position"]
        eid = pp_to_entry.get(pp)
        dec = live_odds_map.get(eid) if eid else None
        if dec is None:
            mip = r["market_implied_prob"]
            dec = (1.0 / mip) if mip and mip > 0 else None
        if dec is None or dec <= 1.0:
            continue
        p = float(r["win_probability"] or 0)
        if p <= 0:
            continue
        b = dec - 1.0
        kf = min(max((b * p - (1 - p)) / b, 0.0), KELLY_CAP)
        stake = kf * BANKROLL
        kelly_stake_total += stake
        if r["official_finish"] == 1:
            kelly_return_total += stake * b
        else:
            kelly_return_total -= stake

    kelly_roi_pct: float | None = None
    if kelly_stake_total > 0:
        kelly_roi_pct = round((kelly_return_total / kelly_stake_total) * 100, 1)

    return {
        "n_results":       n_results,
        "winner":          winner_row["horse_name"] if winner_row else "Unknown",
        "top_pick":        top["horse_name"],
        "top_pick_finish": top_pick_finish,
        "top_pick_won":    top_pick_won,
        # Scratch-aware effective TP fields
        "original_tp_scratched": original_tp_scratched,
        "effective_tp":          eff_top["horse_name"] if eff_top else None,
        "effective_tp_rank":     effective_tp_rank,
        "effective_tp_finish":   effective_tp_finish,
        "effective_tp_won":      effective_tp_won,
        "ml_favorite_name":        ml_fav["horse_name"] if ml_fav else None,
        "post_time_favorite_name": ptf_fav["horse_name"] if ptf_fav else None,
        "post_time_favorite_won":  ptf_won,
        "post_time_favorite_odds": ptf_fav["official_odds_decimal"] if ptf_fav else None,
        "top3_hit":        top3_hit,
        "n_bets":          len(bet_rows),
        "n_bets_won":      len(bet_won),
        "n_bets_itm":      len(bet_itm),
        "kelly_roi_pct":   kelly_roi_pct,
        "kelly_staked":    round(kelly_stake_total, 2),
        "full_results":    data,
    }


_OUTCOMES_SQL = """
WITH base AS (
    SELECT
        sr.run_id, sr.card_id, sr.run_timestamp, sr.model_type,
        COALESCE(sr.chaos_active, sr.derby_override_active, 0) AS chaos_active,
        mr.model_name,
        t.abbrev  AS track_code,
        rc.card_date, rc.race_number, rc.distance_furlongs,
        rc.surface, rc.race_class,
        COALESCE(
            rc.field_size,
            (SELECT COUNT(*) FROM entries e WHERE e.card_id = rc.card_id AND e.scratch_flag = 0)
        ) AS field_size,
        es.rank, es.horse_name, es.win_probability, es.morning_line_odds,
        rr.finish_position, rr.official_finish,
        COALESCE(rr.is_scratched, e.scratch_flag, 0) AS is_scratched,
        COALESCE(rr.is_disqualified, 0)             AS is_disqualified,
        rr.official_odds_decimal
    FROM score_runs sr
    JOIN model_registry mr ON sr.model_id   = mr.model_id
    JOIN race_cards     rc ON sr.card_id     = rc.card_id
    JOIN tracks          t ON rc.track_id    = t.track_id
    JOIN entry_scores   es ON es.run_id      = sr.run_id
    JOIN entries         e ON e.entry_id     = es.entry_id
    LEFT JOIN race_results rr
           ON rr.entry_id = es.entry_id AND rr.card_id = sr.card_id
    WHERE EXISTS (SELECT 1 FROM race_results x WHERE x.card_id = sr.card_id)
),
tp AS (
    -- Original model rank-1 selection; may be scratched
    SELECT run_id,
           horse_name           AS top_pick_name,
           win_probability      AS top_pick_win_prob,
           finish_position      AS top_pick_finish_pos,
           is_scratched         AS original_tp_scratched,
           CASE WHEN official_finish = 1 AND is_disqualified = 0 THEN 1 ELSE 0 END AS top_pick_won
    FROM base WHERE rank = 1
),
eff_tp_rk AS (
    -- First non-scratched entry by model rank, per run
    SELECT run_id, horse_name, rank AS original_rank, finish_position,
           official_finish, is_disqualified,
           ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY rank) AS eff_rk
    FROM base
    WHERE is_scratched = 0
),
eff_tp AS (
    -- Effective top pick: highest-ranked non-scratched entry
    SELECT run_id,
           horse_name      AS effective_tp_name,
           original_rank   AS effective_tp_rank,
           finish_position AS effective_tp_finish,
           CASE WHEN official_finish = 1 AND is_disqualified = 0 THEN 1 ELSE 0 END
               AS effective_tp_won
    FROM eff_tp_rk WHERE eff_rk = 1
),
winner AS (
    SELECT run_id,
           horse_name              AS winner_name,
           official_odds_decimal   AS winner_official_odds
    FROM base
    WHERE official_finish = 1 AND is_disqualified = 0 AND is_scratched = 0
),
ptf_rk AS (
    SELECT run_id, horse_name, official_odds_decimal,
           RANK() OVER (PARTITION BY run_id ORDER BY official_odds_decimal) AS rk
    FROM base
    WHERE official_odds_decimal IS NOT NULL AND is_scratched = 0
),
ptf AS (
    SELECT run_id,
           horse_name              AS post_time_favorite_name,
           official_odds_decimal   AS post_time_favorite_odds
    FROM ptf_rk WHERE rk = 1
),
ml_rk AS (
    SELECT run_id, horse_name, finish_position,
           RANK() OVER (PARTITION BY run_id ORDER BY morning_line_odds) AS rk
    FROM base
    WHERE morning_line_odds IS NOT NULL AND is_scratched = 0
),
mf AS (
    SELECT run_id,
           horse_name      AS ml_favorite_name,
           finish_position AS ml_fav_finish_pos
    FROM ml_rk WHERE rk = 1
),
meta AS (
    SELECT DISTINCT run_id, card_id, run_timestamp, model_type, chaos_active,
                    model_name, track_code, card_date, race_number, distance_furlongs,
                    surface, race_class, field_size
    FROM base
)
SELECT
    meta.track_code,
    meta.card_date                              AS race_date,
    meta.race_number,
    meta.distance_furlongs                      AS distance_f,
    UPPER(SUBSTR(COALESCE(meta.surface,'?'),1,1)) AS surface_code,
    meta.race_class                             AS race_type,
    meta.field_size,
    meta.model_type                             AS quality_tier,
    meta.chaos_active,
    meta.model_name,
    meta.run_timestamp                          AS run_created_at,
    tp.top_pick_name,
    tp.top_pick_win_prob,
    tp.top_pick_finish_pos,
    tp.top_pick_won,
    tp.original_tp_scratched,
    eff_tp.effective_tp_name,
    eff_tp.effective_tp_rank,
    eff_tp.effective_tp_finish,
    eff_tp.effective_tp_won,
    mf.ml_favorite_name,
    mf.ml_fav_finish_pos                        AS ml_favorite_finish_pos,
    CASE WHEN mf.ml_favorite_name = winner.winner_name THEN 1 ELSE 0 END AS ml_favorite_won,
    ptf.post_time_favorite_name,
    ptf.post_time_favorite_odds,
    CASE WHEN ptf.post_time_favorite_name = winner.winner_name THEN 1 ELSE 0 END AS post_time_favorite_won,
    winner.winner_name,
    winner.winner_official_odds
FROM meta
LEFT JOIN tp     ON tp.run_id     = meta.run_id
LEFT JOIN eff_tp ON eff_tp.run_id = meta.run_id
LEFT JOIN mf     ON mf.run_id     = meta.run_id
LEFT JOIN ptf    ON ptf.run_id    = meta.run_id
LEFT JOIN winner ON winner.run_id = meta.run_id
ORDER BY meta.card_date DESC, meta.run_timestamp DESC
LIMIT ?
"""

_OUTCOMES_COLS = [
    "track_code", "race_date", "race_number", "distance_f", "surface_code",
    "race_type", "field_size", "quality_tier", "chaos_active", "model_name", "run_created_at",
    "top_pick_name", "top_pick_win_prob", "top_pick_finish_pos", "top_pick_won",
    "original_tp_scratched",
    "effective_tp_name", "effective_tp_rank", "effective_tp_finish", "effective_tp_won",
    "ml_favorite_name", "ml_favorite_finish_pos", "ml_favorite_won",
    "post_time_favorite_name", "post_time_favorite_odds", "post_time_favorite_won",
    "winner_name", "winner_official_odds",
]


def load_outcomes_frame(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Return one row per (race, score_run) with model vs market outcome data.

    Only races that have race_results ingested are included.
    """
    try:
        rows = conn.execute(_OUTCOMES_SQL, (limit,)).fetchall()
        return [dict(zip(_OUTCOMES_COLS, r)) for r in rows]
    except Exception:
        return []


def delete_results_for_race(conn: sqlite3.Connection, card_id: int) -> int:
    """Delete all race_results for a card. Returns deleted row count."""
    _ensure_table(conn)
    cur = conn.cursor()
    cur.execute("DELETE FROM race_results WHERE card_id=?", (card_id,))
    conn.commit()
    return cur.rowcount


def load_results_summary(conn: sqlite3.Connection, card_id: int) -> dict:
    """Quick summary: does race_results have data for this card?"""
    try:
        _ensure_table(conn)
        row = conn.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN is_scratched=0 THEN 1 ELSE 0 END),
                      MAX(ingested_at)
               FROM race_results WHERE card_id=?""",
            (card_id,),
        ).fetchone()
        if row and row[2]:
            return {
                "n_total":    int(row[0]),
                "n_runners":  int(row[1]),
                "ingested_at": row[2],
            }
    except Exception:
        pass
    return {"n_total": 0, "n_runners": 0, "ingested_at": None}


# ── Race Review view + query helpers ──────────────────────────────────────────

# DDL kept in sync with db/schema.sql and db/migrations/add_race_review_view.sql
_RACE_REVIEW_VIEW_DDL = """
DROP VIEW IF EXISTS race_review;
CREATE VIEW race_review AS
WITH ranked_live AS (
    SELECT
        es.run_id,
        sr.card_id,
        es.entry_id,
        es.horse_name,
        es.rank          AS model_rank,
        es.win_probability,
        es.value_score,
        es.bet_tag,
        -- Scratch flag: prefer race_results (post-ingest), fall back to
        -- entries.scratch_flag so pre-ingest scratches are detected too.
        COALESCE(rr.is_scratched, e.scratch_flag, 0) AS is_scratched,
        ROW_NUMBER() OVER (
            PARTITION BY es.run_id
            ORDER BY
                CASE WHEN COALESCE(rr.is_scratched, e.scratch_flag, 0) = 1 THEN 1 ELSE 0 END,
                es.rank ASC
        ) AS effective_live_rank
    FROM entry_scores es
    JOIN  score_runs    sr  ON sr.run_id   = es.run_id
    JOIN  entries        e  ON e.entry_id  = es.entry_id
    LEFT JOIN race_results rr
           ON rr.entry_id = es.entry_id
          AND rr.card_id  = sr.card_id
),
tops AS (
    SELECT
        rl.run_id,
        rl.card_id,
        MAX(CASE WHEN rl.model_rank = 1 THEN rl.horse_name  END)
            AS original_tp,
        MAX(CASE WHEN rl.model_rank = 1 THEN rl.entry_id    END)
            AS original_tp_entry_id,
        -- Use pre-computed is_scratched from ranked_live (includes entries.scratch_flag)
        MAX(CASE WHEN rl.model_rank = 1 THEN rl.is_scratched END)
            AS original_tp_scratched,
        MAX(CASE WHEN rl.effective_live_rank = 1 THEN rl.horse_name  END)
            AS effective_tp,
        MAX(CASE WHEN rl.effective_live_rank = 1 THEN rl.entry_id    END)
            AS effective_tp_entry_id,
        MAX(CASE WHEN rl.effective_live_rank = 1 THEN rl.model_rank  END)
            AS effective_tp_rank,
        MAX(CASE WHEN rl.effective_live_rank = 1 THEN rr.finish_position END)
            AS effective_tp_finish,
        MAX(CASE WHEN rl.effective_live_rank = 1
                  AND rr.official_finish = 1
                  AND COALESCE(rr.is_disqualified, 0) = 0
                 THEN 1 ELSE 0 END)
            AS effective_tp_won
    FROM ranked_live rl
    LEFT JOIN race_results rr
           ON rr.entry_id = rl.entry_id
          AND rr.card_id  = rl.card_id
    GROUP BY rl.run_id, rl.card_id
)
SELECT
    rc.card_id,
    rc.card_date                                                    AS race_date,
    t.abbrev                                                        AS track,
    rc.race_number,
    rc.surface,
    rc.distance_furlongs,
    CASE WHEN rc.distance_furlongs < 8.5 THEN 'sprint' ELSE 'route' END
                                                                    AS dist_category,
    COALESCE(rc.field_size,
        (SELECT COUNT(*) FROM entries e
         WHERE e.card_id = rc.card_id AND e.scratch_flag = 0))     AS field_size,
    rc.race_class,
    sr.run_id,
    sr.model_id,
    sr.model_type,
    COALESCE(sr.chaos_active, sr.derby_override_active, 0)         AS chaos_active,
    sr.chaos_intensity,
    sr.quality_tier,
    sr.run_timestamp,
    tops.original_tp,
    tops.original_tp_entry_id,
    tops.original_tp_scratched,
    tops.effective_tp,
    tops.effective_tp_entry_id,
    tops.effective_tp_rank,
    tops.effective_tp_finish,
    tops.effective_tp_won,
    winner_h.name                                                   AS actual_winner,
    winner_rr.entry_id                                              AS actual_winner_entry_id
FROM score_runs sr
JOIN  race_cards rc        ON rc.card_id         = sr.card_id
JOIN  tracks     t         ON t.track_id         = rc.track_id
LEFT JOIN tops             ON tops.run_id        = sr.run_id
LEFT JOIN race_results winner_rr
       ON winner_rr.card_id                      = rc.card_id
      AND winner_rr.finish_position              = 1
      AND COALESCE(winner_rr.is_scratched,    0) = 0
      AND COALESCE(winner_rr.is_disqualified, 0) = 0
LEFT JOIN horses winner_h  ON winner_h.horse_id  = winner_rr.horse_id
"""

_REVIEW_COLS = [
    "card_id", "race_date", "track", "race_number", "surface", "distance_furlongs",
    "dist_category", "field_size", "race_class", "run_id", "model_id", "model_type",
    "chaos_active", "chaos_intensity", "quality_tier", "run_timestamp",
    "original_tp", "original_tp_entry_id", "original_tp_scratched",
    "effective_tp", "effective_tp_entry_id", "effective_tp_rank",
    "effective_tp_finish", "effective_tp_won", "actual_winner", "actual_winner_entry_id",
]

_DETAIL_COLS = [
    "model_rank", "horse_name", "post_position", "morning_line_odds",
    "win_probability", "fair_odds", "value_score", "model_edge", "bet_tag",
    "low_conf_bet_block", "is_scratched", "is_disqualified",
    "finish_position", "official_finish", "official_odds_decimal", "beaten_lengths",
    "chaos_score", "chaos_boost", "chaos_tier", "chaos_eligible",
]


def ensure_race_review_view(conn: sqlite3.Connection) -> None:
    """Create (or replace) the race_review view.

    Also ensures chaos columns exist on score_runs and entry_scores so the
    view can reference them.  Idempotent — safe to call on every app start.
    """
    _chaos_ddl = [
        "ALTER TABLE score_runs   ADD COLUMN chaos_active        INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE score_runs   ADD COLUMN chaos_intensity     REAL",
        "ALTER TABLE score_runs   ADD COLUMN field_entropy_score REAL",
        "ALTER TABLE entry_scores ADD COLUMN chaos_score         REAL",
        "ALTER TABLE entry_scores ADD COLUMN chaos_boost         REAL",
        "ALTER TABLE entry_scores ADD COLUMN chaos_tier          TEXT",
        "ALTER TABLE entry_scores ADD COLUMN chaos_eligible      INTEGER NOT NULL DEFAULT 0",
    ]
    for stmt in _chaos_ddl:
        try:
            conn.execute(stmt)
        except Exception:
            pass  # column already exists
    try:
        conn.commit()
    except Exception:
        pass

    # Idempotent startup back-fill: entries absent from race_results for an already-ingested
    # race (one with at least one non-scratched result) were scratched pre-ingest.
    # The is_scratched=0 guard prevents marking all horses as scratched in a race where
    # only a single scratched-horse result row exists (e.g. a scratch notification ingest).
    try:
        conn.execute(
            """UPDATE entries SET scratch_flag = 1
               WHERE scratch_flag = 0
                 AND card_id IN (
                     SELECT DISTINCT card_id FROM race_results WHERE is_scratched = 0
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM race_results rr
                     WHERE rr.entry_id = entries.entry_id
                       AND rr.card_id  = entries.card_id
                 )"""
        )
        conn.commit()
    except Exception:
        pass

    conn.executescript(_RACE_REVIEW_VIEW_DDL)


def load_race_review(
    conn: sqlite3.Connection,
    *,
    date_from:    str | None = None,
    date_to:      str | None = None,
    track:        str | None = None,
    surface:      str | None = None,
    dist_cat:     str | None = None,
    field_min:    int | None = None,
    field_max:    int | None = None,
    model_type:   str | None = None,
    chaos_active: int | None = None,
    with_results: bool = False,
    limit: int = 250,
) -> list[dict]:
    """Query race_review with optional filters.

    Returns one dict per (race, score_run), most-recent first.
    When with_results=True only rows where actual_winner is populated
    (i.e. race_results have been ingested) are returned.
    """
    ensure_race_review_view(conn)
    clauses: list[str] = []
    params:  list      = []

    if with_results:
        clauses.append("actual_winner IS NOT NULL")
    if date_from:
        clauses.append("race_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("race_date <= ?")
        params.append(date_to)
    if track:
        clauses.append("track = ?")
        params.append(track)
    if surface:
        clauses.append("surface = ?")
        params.append(surface)
    if dist_cat:
        clauses.append("dist_category = ?")
        params.append(dist_cat)
    if field_min is not None:
        clauses.append("field_size >= ?")
        params.append(field_min)
    if field_max is not None:
        clauses.append("field_size <= ?")
        params.append(field_max)
    if model_type:
        clauses.append("model_type = ?")
        params.append(model_type)
    if chaos_active is not None:
        clauses.append("chaos_active = ?")
        params.append(int(chaos_active))

    where  = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    # Deduplicate: return only the most-recent score_run per race card.
    # _rn is intentionally excluded from _REVIEW_COLS so zip drops it.
    sql    = (
        f"SELECT * FROM ("
        f"  SELECT *, ROW_NUMBER() OVER"
        f"    (PARTITION BY card_id ORDER BY run_timestamp DESC) AS _rn"
        f"  FROM race_review {where}"
        f") WHERE _rn = 1"
        f" ORDER BY race_date DESC, run_timestamp DESC LIMIT ?"
    )
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(zip(_REVIEW_COLS, r)) for r in rows]
    except Exception:
        return []


def load_race_detail(
    conn: sqlite3.Connection,
    run_id: str,
    card_id: int,
) -> list[dict]:
    """Return one row per runner merging entry_scores + race_results for one run.

    Ordered by model rank.  Result columns are NULL for runners not matched in
    race_results; is_scratched / is_disqualified default to 0 via COALESCE.
    """
    rows = conn.execute(
        """SELECT
               es.rank                              AS model_rank,
               es.horse_name,
               es.post_position,
               es.morning_line_odds,
               es.win_probability,
               es.fair_odds,
               es.value_score,
               es.model_edge,
               es.bet_tag,
               es.low_conf_bet_block,
               COALESCE(rr.is_scratched, e.scratch_flag, 0) AS is_scratched,
               COALESCE(rr.is_disqualified, 0)              AS is_disqualified,
               rr.finish_position,
               rr.official_finish,
               rr.official_odds_decimal,
               rr.beaten_lengths,
               es.chaos_score,
               es.chaos_boost,
               es.chaos_tier,
               COALESCE(es.chaos_eligible,  0)     AS chaos_eligible
           FROM entry_scores es
           JOIN  entries e ON e.entry_id = es.entry_id
           LEFT JOIN race_results rr
                  ON rr.entry_id = es.entry_id AND rr.card_id = ?
           WHERE es.run_id = ?
           ORDER BY es.rank""",
        (card_id, run_id),
    ).fetchall()
    return [dict(zip(_DETAIL_COLS, r)) for r in rows]
