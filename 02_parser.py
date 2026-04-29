"""
DerbyEdge Engine — Equibase Chart PDF Parser
Parses downloaded Equibase result chart PDFs into structured CSVs.

Outputs three CSV types per source PDF, following naming convention:
  data/raw/historical_results/ YYYY/MM/DD/ eqb_{TRACK}_{DATE}.csv
  data/raw/past_performances/  YYYY/MM/DD/ eqb_{TRACK}_{DATE}_pp.csv
  data/raw/workouts/           YYYY/MM/DD/ eqb_{TRACK}_{DATE}_wk.csv
"""

import re
import csv
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import pdfplumber
import pandas as pd
from tqdm import tqdm

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

RAW_BASE        = Path(r"C:\Projects\derbyedge-engine\data\raw")
RESULTS_IN      = RAW_BASE / "historical_results"
RESULTS_OUT     = RAW_BASE / "historical_results"
PP_OUT          = RAW_BASE / "past_performances"
WORKOUTS_OUT    = RAW_BASE / "workouts"

SOURCE_PREFIX   = "eqb"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("parser.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DATA MODELS
# ---------------------------------------------------------------------------

@dataclass
class StarterRow:
    """One horse's result in a single race."""
    source:          str = ""
    track_code:      str = ""
    race_date:       str = ""
    race_num:        int = 0
    surface:         str = ""
    distance:        str = ""
    race_type:       str = ""
    conditions:      str = ""
    purse:           str = ""
    weather:         str = ""
    track_condition: str = ""
    off_time:        str = ""
    final_time:      str = ""
    frac_times:      str = ""   # comma-separated fractional splits
    program:         str = ""
    horse_name:      str = ""
    jockey:          str = ""
    trainer:         str = ""
    weight:          str = ""
    medication:      str = ""
    equipment:       str = ""
    post_position:   int = 0
    start_pos:       str = ""
    call_q1:         str = ""   # 1/4
    call_half:       str = ""   # 1/2
    call_str:        str = ""   # stretch
    call_fin:        str = ""   # finish
    finish_pos:      int = 0
    beaten_lengths:  str = ""
    odds:            str = ""
    win_pay:         str = ""
    place_pay:       str = ""
    show_pay:        str = ""
    comments:        str = ""
    winner:          str = ""
    breeder:         str = ""
    owner:           str = ""


@dataclass
class PPRow:
    """One past-performance line for a horse (from the PP Running Line Preview table)."""
    source:      str = ""
    track_code:  str = ""
    race_date:   str = ""
    race_num:    int = 0
    horse_name:  str = ""
    pp_program:  str = ""
    start_pos:   str = ""
    call_q1:     str = ""
    call_half:   str = ""
    call_str:    str = ""
    call_fin:    str = ""


@dataclass
class WorkoutRow:
    """A workout entry parsed from a chart footnote or workout table."""
    source:     str = ""
    track_code: str = ""
    work_date:  str = ""
    horse_name: str = ""
    distance:   str = ""
    time:       str = ""
    surface:    str = ""
    notes:      str = ""


# ---------------------------------------------------------------------------
# REGEX PATTERNS
# ---------------------------------------------------------------------------

RE_RACE_HEADER   = re.compile(r"RACE\s*#?\s*(\d+)", re.IGNORECASE)
RE_TRACK_DATE    = re.compile(
    r"([A-Z ]+)\s*[–\-]\s*([A-Z][a-z]+ \d{1,2},\s*\d{4})\s*[–\-]\s*Race\s*(\d+)",
    re.IGNORECASE,
)
RE_SURFACE       = re.compile(r"\b(Dirt|Turf|Synthetic|Tapeta|Polytrack|All Weather)\b", re.IGNORECASE)
RE_DISTANCE      = re.compile(r"((?:\d+\s+)?(?:\d+/\d+)\s+Furlongs?|(?:\d+)\s+Miles?|\d+\s+Yards?|About\s+[\d/\s]+(?:Furlongs?|Miles?))", re.IGNORECASE)
RE_PURSE         = re.compile(r"Purse:\s*\$?([\d,]+)", re.IGNORECASE)
RE_WEATHER       = re.compile(r"Weather:\s*([^;|\n]+)", re.IGNORECASE)
RE_TRACK_COND    = re.compile(r"Track:\s*([A-Za-z]+)", re.IGNORECASE)
RE_OFF_TIME      = re.compile(r"Off\s+at:\s*([\d:]+)", re.IGNORECASE)
RE_FINAL_TIME    = re.compile(r"Final\s+Time[:\s]+([\d:\.]+)", re.IGNORECASE)
RE_FRAC_TIMES    = re.compile(r"Fractional\s+Times?[:\s]+([\d\s\.:]+)", re.IGNORECASE)
RE_WINNER        = re.compile(r"Winner:\s*([^\n,]+)", re.IGNORECASE)
RE_TRAINER       = re.compile(r"Trainer:\s*([^\n]+)", re.IGNORECASE)
RE_BREEDER       = re.compile(r"Breeder:\s*([^\n]+)", re.IGNORECASE)
RE_OWNER         = re.compile(r"Owner:\s*([^\n]+)", re.IGNORECASE)
RE_RACE_TYPE     = re.compile(
    r"^(CLAIMING|MAIDEN CLAIMING|MAIDEN SPECIAL WEIGHT|ALLOWANCE|"
    r"STAKES|LISTED STAKES|GRADED STAKES|STARTER ALLOWANCE|"
    r"STARTER HANDICAP|HANDICAP|OPTIONAL CLAIMING|WAIVER CLAIMING|"
    r"MAIDEN|MATCH RACE|WALKOVER)\b",
    re.IGNORECASE | re.MULTILINE,
)
RE_STARTER_ROW   = re.compile(
    # Matches lines like: "  1  Back Stop (Collins, Denree)  104 BL 0 1  head  1/1/0  ..."
    # Groups: program, name (jockey), weight, meds, equipment, pp, ... positions ... odds comments
    r"^\s*(\d{1,2}[A-Z]?)\s+"           # program number
    r"(.+?)\s*\(([^)]+)\)\s+"           # horse name (jockey)
    r"(\d{3})\s*"                        # weight
    r"([A-Z]{0,3})\s*"                  # medication/equipment codes
    r"(\d{1,2})\s+"                     # post position
    r"(\d{1,2})\s+"                     # start call
    r"([\d\s\^head nk]+?)\s+"           # running calls (loose)
    r"(\d{1,2})\s+"                     # finish position
    r"([\d\.]+)\s*"                      # odds
    r"(.*)$",                            # comments
    re.IGNORECASE,
)
RE_WPS_TABLE     = re.compile(
    r"^\s*(\d{1,2}[A-Z]?)\s+"           # program
    r"(.+?)\s+"                          # horse
    r"([\d\.]+)\s+"                      # win
    r"([\d\.]+)\s+"                      # place
    r"([\d\.]+)",                        # show
    re.MULTILINE,
)
RE_PP_ROW        = re.compile(
    r"^\s*(\d{1,2}[A-Z]?)\s+"           # program
    r"(.+?)\s+"                          # horse
    r"(\d{1,2})\s+"                     # start
    r"([\d\^]+)\s+"                     # q1
    r"([\d\^]+)\s+"                     # half
    r"([\d\^]+)\s+"                     # str
    r"([\d\^]+)\s*$",                   # fin
    re.MULTILINE,
)
RE_WORKOUT       = re.compile(
    r"([A-Za-z \-\']+)\s+"             # horse name
    r"(\w{2,4})\s+"                    # track code
    r"(\w+)\s+"                        # date
    r"([\d:\.]+)\s+"                   # time
    r"([BHg])\s*"                      # surface code
    r"([\d/]+)\s*"                     # distance
    r"(.+?)(?:\s+\d+)?$",             # notes
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# PDF TEXT EXTRACTION
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract all text from a PDF, page by page, joined with newlines."""
    pages = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text(x_tolerance=2, y_tolerance=3)
                if text:
                    pages.append(text)
    except Exception as e:
        log.error(f"pdfplumber error on {pdf_path.name}: {e}")
    return "\n".join(pages)


# ---------------------------------------------------------------------------
# PARSING HELPERS
# ---------------------------------------------------------------------------

def safe_group(m, n, strip=True):
    if m is None:
        return ""
    try:
        val = m.group(n)
        return val.strip() if strip and val else (val or "")
    except IndexError:
        return ""


def parse_race_metadata(text: str, track_code: str, race_date: str, race_num: int) -> dict:
    """Extract race-level metadata from chart text."""
    meta = {
        "source":          SOURCE_PREFIX,
        "track_code":      track_code,
        "race_date":       race_date,
        "race_num":        race_num,
        "surface":         safe_group(RE_SURFACE.search(text), 1),
        "distance":        safe_group(RE_DISTANCE.search(text), 0),
        "race_type":       safe_group(RE_RACE_TYPE.search(text), 0).upper(),
        "purse":           safe_group(RE_PURSE.search(text), 1),
        "weather":         safe_group(RE_WEATHER.search(text), 1),
        "track_condition": safe_group(RE_TRACK_COND.search(text), 1),
        "off_time":        safe_group(RE_OFF_TIME.search(text), 1),
        "final_time":      safe_group(RE_FINAL_TIME.search(text), 1),
        "frac_times":      "",
        "winner":          safe_group(RE_WINNER.search(text), 1),
        "trainer":         safe_group(RE_TRAINER.search(text), 1),
        "breeder":         safe_group(RE_BREEDER.search(text), 1),
        "owner":           safe_group(RE_OWNER.search(text), 1),
        "conditions":      "",
    }

    # Fractional times — collapse whitespace
    frac_m = RE_FRAC_TIMES.search(text)
    if frac_m:
        meta["frac_times"] = re.sub(r"\s+", " ", frac_m.group(1)).strip()

    # Race conditions: grab multi-line block between race type and "Purse:"
    cond_m = re.search(
        r"(CLAIMING|MAIDEN|ALLOWANCE|STAKES|HANDICAP|STARTER)[^\n]*\n((?:.+\n){0,5}?)Purse:",
        text, re.IGNORECASE
    )
    if cond_m:
        meta["conditions"] = re.sub(r"\s+", " ", cond_m.group(2)).strip()

    return meta


def parse_starters(text: str, meta: dict) -> list[StarterRow]:
    """
    Parse individual starter lines from chart text.
    Falls back to a table-extraction approach when regex line matching yields < 2 starters.
    """
    rows = []

    # --- Approach 1: line-by-line regex ---
    for line in text.splitlines():
        m = RE_STARTER_ROW.match(line)
        if not m:
            continue
        row = StarterRow(
            source          = meta["source"],
            track_code      = meta["track_code"],
            race_date       = meta["race_date"],
            race_num        = meta["race_num"],
            surface         = meta["surface"],
            distance        = meta["distance"],
            race_type       = meta["race_type"],
            conditions      = meta["conditions"],
            purse           = meta["purse"],
            weather         = meta["weather"],
            track_condition = meta["track_condition"],
            off_time        = meta["off_time"],
            final_time      = meta["final_time"],
            frac_times      = meta["frac_times"],
            winner          = meta["winner"],
            trainer         = meta["trainer"],
            breeder         = meta["breeder"],
            owner           = meta["owner"],
            program         = m.group(1).strip(),
            horse_name      = m.group(2).strip(),
            jockey          = m.group(3).strip(),
            weight          = m.group(4).strip(),
            medication      = m.group(5).strip(),
            post_position   = _safe_int(m.group(6)),
            start_pos       = m.group(7).strip(),
            comments        = m.group(11).strip() if m.lastindex >= 11 else "",
        )

        # Parse running calls from loose group 8
        calls = m.group(8).split() if m.group(8) else []
        call_labels = ["call_q1", "call_half", "call_str"]
        for i, label in enumerate(call_labels):
            setattr(row, label, calls[i] if i < len(calls) else "")

        row.finish_pos = _safe_int(m.group(9))
        row.odds       = m.group(10).strip() if m.lastindex >= 10 else ""
        rows.append(row)

    # --- Approach 2: pdfplumber table extraction (for well-formatted tables) ---
    # This runs as supplement if regex caught fewer than expected starters
    # (handled at call site via parse_pdf_with_tables)

    return rows


def parse_wps(text: str,