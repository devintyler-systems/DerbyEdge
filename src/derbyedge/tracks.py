"""
src/derbyedge/tracks.py

Canonical track registry and name resolver for DerbyEdge.

Public API
----------
normalize_track_name(name: str) -> str
    Lowercase, strip punctuation, collapse spaces.

resolve_track(track_name=None, track_code=None) -> dict
    Returns {track_code, track_name_canonical, resolution_source}.
    resolution_source: "parsed_code" | "alias_exact" | "alias_fuzzy" | "unresolved"

TRACK_CODES: dict[str, str]
    Flat mapping of normalized alias fragments → track codes.
    Imported by pdf_ingest.py for substring-scan extraction; do not remove.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
import re
from typing import Optional


def normalize_track_name(name: str) -> str:
    """Lowercase, strip punctuation (keep digits/spaces), collapse whitespace."""
    s = name.strip().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass(frozen=True)
class _TrackRecord:
    code: str
    name: str
    aliases: tuple[str, ...]


_TRACKS: tuple[_TrackRecord, ...] = (
    _TrackRecord("CD",  "Churchill Downs",       ("Churchill Downs", "Churchill")),
    _TrackRecord("PIM", "Pimlico",               ("Pimlico",)),
    _TrackRecord("BEL", "Belmont Park",          ("Belmont Park", "Belmont")),
    _TrackRecord("KEE", "Keeneland",             ("Keeneland",)),
    _TrackRecord("SA",  "Santa Anita Park",      ("Santa Anita Park", "Santa Anita")),
    _TrackRecord("GP",  "Gulfstream Park",       ("Gulfstream Park", "Gulfstream")),
    _TrackRecord("AQU", "Aqueduct",              ("Aqueduct",)),
    _TrackRecord("DMR", "Del Mar",               ("Del Mar",)),
    _TrackRecord("SAR", "Saratoga",              ("Saratoga",)),
    _TrackRecord("OP",  "Oaklawn Park",          ("Oaklawn Park", "Oaklawn")),
    _TrackRecord("FG",  "Fair Grounds",          ("Fair Grounds",)),
    _TrackRecord("TP",  "Turfway Park",          ("Turfway Park", "Turfway")),
    _TrackRecord("WO",  "Woodbine",              ("Woodbine",)),
    _TrackRecord("GG",  "Golden Gate Fields",    ("Golden Gate Fields", "Golden Gate")),
    _TrackRecord("MTH", "Monmouth Park",         ("Monmouth Park", "Monmouth")),
    _TrackRecord("PEN", "Penn National",         ("Penn National", "Hollywood Casino at Penn National")),
    _TrackRecord("PRX", "Parx Racing",           ("Parx Racing", "Parx")),
    _TrackRecord("LRL", "Laurel Park",           ("Laurel Park", "Laurel")),
    _TrackRecord("TAM", "Tampa Bay Downs",       ("Tampa Bay Downs", "Tampa Bay")),
    _TrackRecord("CT",  "Charles Town Races",    ("Charles Town Races", "Charles Town")),
    _TrackRecord("RP",  "Remington Park",        ("Remington Park", "Remington")),
    _TrackRecord("HAW", "Hawthorne Race Course", ("Hawthorne Race Course", "Hawthorne")),
    _TrackRecord("CNL", "Colonial Downs",        ("Colonial Downs", "Colonial")),
    _TrackRecord("SUF", "Suffolk Downs",         ("Suffolk Downs", "Suffolk")),
    _TrackRecord("FL",  "Finger Lakes",          ("Finger Lakes",)),
    _TrackRecord("PID", "Presque Isle Downs",    ("Presque Isle Downs", "Presque Isle")),
    _TrackRecord("EVD", "Evangeline Downs",      ("Evangeline Downs", "Evangeline")),
    _TrackRecord("IND", "Horseshoe Indianapolis", (
        "Horseshoe Indianapolis",
        "Indiana Grand",
        "Indiana Grand Racing & Casino",
        "Indiana Downs",
    )),
)

# Internal lookups built at import time
_CODE_TO_NAME: dict[str, str] = {}
_ALIAS_TO_CODE: dict[str, str] = {}

for _rec in _TRACKS:
    _CODE_TO_NAME[_rec.code] = _rec.name
    _ALIAS_TO_CODE[normalize_track_name(_rec.name)] = _rec.code
    for _alias in _rec.aliases:
        _ALIAS_TO_CODE[normalize_track_name(_alias)] = _rec.code

# Flat fragment dict exported for pdf_ingest.py substring-scan functions.
# Every normalized alias becomes a key; short aliases (e.g. "santa anita",
# "indiana grand") act as natural substrings of PDF header text.
TRACK_CODES: dict[str, str] = dict(_ALIAS_TO_CODE)


def resolve_track(
    track_name: Optional[str] = None,
    track_code: Optional[str] = None,
) -> dict:
    """Resolve a parsed track name or code to a canonical registry entry.

    Priority: explicit code > alias exact match > alias fuzzy match.

    Returns:
        {
          "track_code":           str | None,
          "track_name_canonical": str | None,
          "resolution_source":    "parsed_code" | "alias_exact"
                                  | "alias_fuzzy" | "unresolved",
        }
    """
    if track_code:
        code = track_code.strip().upper()
        if code in _CODE_TO_NAME:
            return {
                "track_code":           code,
                "track_name_canonical": _CODE_TO_NAME[code],
                "resolution_source":    "parsed_code",
            }

    if track_name:
        norm = normalize_track_name(track_name)

        code = _ALIAS_TO_CODE.get(norm)
        if code:
            return {
                "track_code":           code,
                "track_name_canonical": _CODE_TO_NAME[code],
                "resolution_source":    "alias_exact",
            }

        matches = get_close_matches(norm, list(_ALIAS_TO_CODE.keys()), n=1, cutoff=0.85)
        if matches:
            code = _ALIAS_TO_CODE[matches[0]]
            return {
                "track_code":           code,
                "track_name_canonical": _CODE_TO_NAME[code],
                "resolution_source":    "alias_fuzzy",
            }

    return {
        "track_code":           None,
        "track_name_canonical": None,
        "resolution_source":    "unresolved",
    }
