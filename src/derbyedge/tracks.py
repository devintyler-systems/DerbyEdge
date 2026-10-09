"""
src/derbyedge/tracks.py

Canonical track registry and name resolver for DerbyEdge.

Public API
----------
normalize_track_name(name: str) -> str
    Lowercase, strip punctuation, collapse spaces.

resolve_track(track_name=None, track_code=None) -> dict
    Returns {track_code, track_name_canonical, resolution_source, kind}.
    resolution_source: "parsed_code" | "alias_exact" | "alias_normalized"
                       | "alias_fuzzy" | "ambiguous" | "unresolved"
    kind: RACETRACK | FAIR | FARM | TRAINING (None when unresolved)

TRACK_CODES: dict[str, str]
    Flat mapping of normalized alias fragments → track codes for the curated
    tracks only.  Imported by pdf_ingest.py for substring-scan extraction; it is
    deliberately NOT widened with the full registry (short names such as "Ely"
    or "Peg" would create false positives when scanning PDF header text).

Registry data (data/reference/)
-------------------------------
equibase_track_abbreviations.csv  342 Equibase codes (racetracks, fairs, farms,
                                  training centres) from the Equibase
                                  "North American Racetrack Abbreviations" list.
track_additions.csv               facilities missing from the Equibase list (operator-supplied codes).
track_aliases.csv                 extra spellings DraftKings uses -> code.
track_timezones.csv               IANA timezone per code (post time -> UTC).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from difflib import get_close_matches
import re
import unicodedata
from pathlib import Path
from typing import Optional


def normalize_track_name(name: str) -> str:
    """Lowercase, strip punctuation (keep digits/spaces), collapse whitespace."""
    s = name.strip().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_track_text(text: str) -> str:
    """Uppercase, strip OCR/punctuation noise, collapse whitespace.

    Designed for matching against noisy PDF header text where artifacts like
    "LOUISIANA? DOWNS" should resolve to "LOUISIANA DOWNS".  Unlike
    normalize_track_name(), this produces an UPPERCASE canonical form used
    exclusively by the PDF-header alias scan (TRACK_CODES_UPPER).
    """
    s = unicodedata.normalize("NFKD", text)
    s = s.upper()
    s = re.sub(r"[^A-Z0-9\s]", " ", s)
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
    _TrackRecord("SAR", "Saratoga", (
        "Saratoga",
        "Saratoga Race Course",
        "Saratoga Racetrack",
    )),
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
    _TrackRecord("LAD", "Louisiana Downs", (
        "Louisiana Downs",
        "Louisiana Downs Racetrack",
        "Louisiana Downs Bossier City",
    )),
    _TrackRecord("IND", "Horseshoe Indianapolis", (
        "Horseshoe Indianapolis",
        "Indiana Grand",
        "Indiana Grand Racing & Casino",
        "Indiana Downs",
    )),
    _TrackRecord("PRM", "Prairie Meadows", (
        "Prairie Meadows",
        "Prairie Meadows Racetrack",
        "Prairie Meadows Racetrack and Casino",
        # Legacy / operator alias used before canonical code was registered
        "PRA",
    )),
    _TrackRecord("MNR", "Mountaineer", (
        "Mountaineer Casino Racetrack & Resort",
        "Mountaineer Casino Racetrack and Resort",
        "Mountaineer Casino Racetrack",
        "Mountaineer Casino",
        "Mountaineer",
    )),
)

# ---------------------------------------------------------------------------
# Curated tracks (authoritative names / aliases) + data-file registry
# ---------------------------------------------------------------------------
_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "reference"
RACE_VENUE_KINDS = frozenset({"RACETRACK", "FAIR"})


def _read_reference(name: str) -> list[dict[str, str]]:
    path = _DATA_DIR / name
    if not path.is_file():
        raise FileNotFoundError(
            f"track registry data file missing: {path} (it is part of the repository)"
        )
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@dataclass
class _Entry:
    code: str
    name: str
    kind: str
    aliases: set
    state: str = ""
    location: str = ""
    note: str = ""
    sources: set = None

    def __post_init__(self):
        if self.sources is None:
            self.sources = set()


_ENTRIES: dict[str, _Entry] = {}
for _rec in _TRACKS:
    _ENTRIES[_rec.code] = _Entry(_rec.code, _rec.name, "RACETRACK", {_rec.name, *_rec.aliases})

# Curated-only flat dicts, kept exactly as before for pdf_ingest's substring scan.
_CODE_TO_NAME: dict[str, str] = {r.code: r.name for r in _TRACKS}
_ALIAS_TO_CODE: dict[str, str] = {}
for _rec in _TRACKS:
    _ALIAS_TO_CODE[normalize_track_name(_rec.name)] = _rec.code
    for _alias in _rec.aliases:
        _ALIAS_TO_CODE[normalize_track_name(_alias)] = _rec.code
TRACK_CODES: dict[str, str] = dict(_ALIAS_TO_CODE)
TRACK_CODES_UPPER: dict[str, str] = {}
for _rec in _TRACKS:
    TRACK_CODES_UPPER[normalize_track_text(_rec.name)] = _rec.code
    for _alias in _rec.aliases:
        TRACK_CODES_UPPER[normalize_track_text(_alias)] = _rec.code

for _row in _read_reference("equibase_track_abbreviations.csv"):
    _code, _name, _kind = _row["code"].strip().upper(), _row["name"].strip(), _row["kind"].strip().upper()
    if _code in _ENTRIES:
        _ENTRIES[_code].aliases.add(_name)       # curated name stays canonical
    else:
        _ENTRIES[_code] = _Entry(_code, _name, _kind, {_name})
# Operator-supplied additions: facilities DraftKings prints that the Equibase PDF predates or omits.
for _row in _read_reference("track_additions.csv"):
    _code, _name, _kind = _row["code"].strip().upper(), _row["name"].strip(), _row["kind"].strip().upper()
    if _code in _ENTRIES:
        raise ValueError(f"track_additions.csv code {_code!r} already exists in the registry")
    _ENTRIES[_code] = _Entry(_code, _name, _kind, {_name})
for _row in _read_reference("track_aliases.csv"):
    _code = _row["code"].strip().upper()
    if _code not in _ENTRIES:
        raise ValueError(f"track_aliases.csv maps {_row['alias']!r} to unknown code {_code!r}")
    _ENTRIES[_code].aliases.add(_row["alias"].strip())

for _code, _entry in _ENTRIES.items():
    _entry.sources.add("curated" if _code in _CODE_TO_NAME else "equibase_or_additions")

# Name keys at three strictness levels.  DraftKings writes "TRAINING CENTER"
# where Equibase writes "TC", "FARMS" for "FARM", "RACE COURSE" for "RACECOURSE".
#   0  punctuation-stripped lowercase name
#   1  Equibase abbreviation spellings expanded (TC, Th'ghbred, Bros, ...)
#   2  generic facility words dropped (race course, training center, farm, ...)
_L1_RULES = (
    (re.compile(r"\bth ?ghbred\b|\bthoro ?bred\b"), "thoroughbred"),
    (re.compile(r"\bc nter\b|\bcentre\b"), "center"),
    (re.compile(r"\btc\b|\bt c\b"), "training center"),
    (re.compile(r"\bbros\b"), "brothers"),
    (re.compile(r"\bfarms\b"), "farm"),
    (re.compile(r"\bstables\b"), "stable"),
    (re.compile(r"\bfairgr nds\b"), "fairgrounds"),
    (re.compile(r"\brace track\b"), "racetrack"),
    (re.compile(r"\brace course\b"), "racecourse"),
    (re.compile(r"\b(?:inc|llc)\b"), ""),
)
_L2_RULES = tuple(re.compile(p) for p in (
    r" and training center$", r" training center$", r" training$", r" racecourse$",
    r" racetrack$", r" races$", r" farm$", r" stable$", r"^the ",
))


def _tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _name_keys(name: str) -> tuple[str, str, str]:
    k0 = normalize_track_name(name)
    k1 = k0
    for pattern, repl in _L1_RULES:
        k1 = pattern.sub(repl, k1)
    k1 = _tidy(k1)
    k2 = k1
    for pattern in _L2_RULES:
        k2 = _tidy(pattern.sub("", k2))
    return k0, k1, k2


_INDEX: tuple[dict[str, set], dict[str, set], dict[str, set]] = ({}, {}, {})


_PRIMARY_INDEX: tuple[dict[str, set], dict[str, set], dict[str, set]] | None = None


def _index_name(code: str, text: str, *, secondary: bool) -> bool:
    """Register a name for a code.

    ``secondary`` names (the track listing and the Equineline directory) are refused when a
    primary source (curated, Equibase list, operator additions / aliases) already holds the
    name at ANY level, so adding a source can never change what an existing name resolves to.
    Two secondary codes sharing a name stay ambiguous rather than the first one winning."""
    keys = _name_keys(text)
    if secondary and _PRIMARY_INDEX is not None and any(
        k and (_PRIMARY_INDEX[lvl].get(k, set()) - {code}) for lvl, k in enumerate(keys)
    ):
        return False
    for lvl, k in enumerate(keys):
        if k:
            _INDEX[lvl].setdefault(k, set()).add(code)
    return True


# Priority 1: curated + Equibase list + operator additions + operator aliases (may be ambiguous
# with each other on purpose, e.g. "Eclipse Farm" / "Eclipse TC" at the loosest level).
for _entry in _ENTRIES.values():
    for _alias in {_entry.name, *_entry.aliases}:
        _index_name(_entry.code, _alias, secondary=False)

_PRIMARY_INDEX = tuple({k: set(v) for k, v in level.items()} for level in _INDEX)
_P1_NAMES: dict[str, set] = {_c: {_e.name, *_e.aliases} for _c, _e in _ENTRIES.items()}
_UNREGISTERED_NAMES: list[tuple[str, str, str]] = []     # (code, name, source) refused as already claimed

_PSEUDO = re.compile(
    r"\b(special|pick \d|pick four|pick five|double|crossover|wagers?|futures?|best bets|multiple tracks|coast to coast)\b",
    re.I,
)


def _classify(name: str) -> str:
    if _PSEUDO.search(name):
        return "OTHER"
    if re.search(r"County Fair\b|\bFair$|\(Fair\)|Fairgr|\bCF\b", name, re.I):
        return "FAIR"
    if re.search(r"\bTC\b|T\.C\.|Training|Equine C|Trng|\bTrain\b", name, re.I):
        return "TRAINING"
    if re.search(r"\b(Farm|Farms|Ranch|Stable|Stables|Stud)\b", name, re.I):
        return "FARM"
    return "RACETRACK"


def _display(name: str) -> str:
    """Title-case an all-caps source name (Equineline) without mangling apostrophes."""
    if name != name.upper():
        return name
    return re.sub(r"[A-Za-z]+(?:'[A-Za-z]+)?", lambda m: m.group(0).capitalize(), name)


_ST = {"AL": "AL", "AK": "AK", "AZ": "AZ", "AR": "AR", "CA": "CA", "CO": "CO", "CT": "CT", "DE": "DE", "FL": "FL",
       "GA": "GA", "HI": "HI", "ID": "ID", "IL": "IL", "IN": "IN", "IA": "IA", "KS": "KS", "KY": "KY", "LA": "LA",
       "ME": "ME", "MD": "MD", "MA": "MA", "MI": "MI", "MN": "MN", "MS": "MS", "MO": "MO", "MT": "MT", "NE": "NE",
       "NV": "NV", "NH": "NH", "NJ": "NJ", "NM": "NM", "NY": "NY", "NC": "NC", "ND": "ND", "OH": "OH", "OK": "OK",
       "OR": "OR", "PA": "PA", "RI": "RI", "SC": "SC", "SD": "SD", "TN": "TN", "TX": "TX", "UT": "UT", "VT": "VT",
       "VA": "VA", "WA": "WA", "WV": "WV", "WI": "WI", "WY": "WY", "PR": "PR", "CAN": "CAN", "MEX": "MEX"}


def _merge_source(rows: list[dict], source: str, *, name_key: str) -> None:
    """Fold a lower-priority source in.  Existing codes only gain metadata and any extra
    spelling nobody else owns; unseen codes become new entries."""
    for row in rows:
        code = row["code"].strip().upper()
        raw_name = row[name_key].strip()
        entry = _ENTRIES.get(code)
        if entry is None:
            entry = _Entry(code, _display(raw_name), _classify(raw_name), set())
            _ENTRIES[code] = entry
        entry.sources.add(source)
        if not _index_name(code, raw_name, secondary=True):
            _UNREGISTERED_NAMES.append((code, raw_name, source))
        else:
            entry.aliases.add(raw_name)
        if source == "listing":
            entry.state = entry.state or _ST.get(row["state"].strip(), row["state"].strip())
        else:
            entry.location = entry.location or row["location"].strip().title()
            entry.note = entry.note or row["note"].strip()


_merge_source(_read_reference("source_listing_tracks.csv"), "listing", name_key="name")
_equineline_rows: dict[str, dict] = {}
for _row in _read_reference("source_equineline_tracks.csv"):
    _equineline_rows.setdefault(_row["code"], _row)       # LP / SND repeat; the first spelling stands
_merge_source(sorted(_equineline_rows.values(), key=lambda r: r["code"]), "equineline", name_key="name")

# Timezones: hand-maintained file wins over the ones derived from the listing.
_TZ: dict[str, str] = {}
for _fname in ("track_timezones_derived.csv", "track_timezones.csv"):
    for _row in _read_reference(_fname):
        _code = _row["code"].strip().upper()
        if _code not in _ENTRIES:
            raise ValueError(f"{_fname} has unknown code {_code!r}")
        _TZ[_code] = _row["timezone"].strip()

# Fuzzy matching only looks at the priority-1 spellings of racetracks and fairs, so adding a
# lower-priority source cannot create new fuzzy hits.
_FUZZY_KEYS: dict[str, str] = {
    _name_keys(_a)[0]: _code
    for _code, _names in _P1_NAMES.items() if _ENTRIES[_code].kind in RACE_VENUE_KINDS
    for _a in _names
}


def get_track(track_code: Optional[str]) -> Optional[dict]:
    """Registry entry for a canonical code: {code, name, kind, timezone}, or None."""
    entry = _ENTRIES.get((track_code or "").strip().upper())
    if entry is None:
        return None
    return {
        "code": entry.code, "name": entry.name, "kind": entry.kind, "timezone": _TZ.get(entry.code),
        "state": entry.state, "location": entry.location, "note": entry.note,
        "sources": sorted(entry.sources),
    }


def is_race_venue(track_code: Optional[str]) -> bool:
    """True when the code is a racetrack or fair (a place a race card can be run)."""
    entry = _ENTRIES.get((track_code or "").strip().upper())
    return entry is not None and entry.kind in RACE_VENUE_KINDS


def track_timezone(track_code: Optional[str]) -> Optional[str]:
    """IANA timezone for a canonical track code, or None when not registered."""
    return _TZ.get((track_code or "").strip().upper())


def registry_size() -> int:
    return len(_ENTRIES)


def _hit(code: str, source: str) -> dict:
    entry = _ENTRIES[code]
    return {
        "track_code": code,
        "track_name_canonical": entry.name,
        "resolution_source": source,
        "kind": entry.kind,
    }


_UNRESOLVED = {
    "track_code": None, "track_name_canonical": None, "resolution_source": "unresolved", "kind": None,
}


def resolve_track(
    track_name: Optional[str] = None,
    track_code: Optional[str] = None,
) -> dict:
    """Resolve a parsed track name or code to a canonical registry entry.

    Priority: explicit code > exact name > Equibase-spelling variants > bare
    code written as a name ("BEL") > generic-word-stripped name > fuzzy name.
    A name that matches more than one code at the same level is "ambiguous"
    and is never guessed.
    """
    if track_code:
        code = track_code.strip().upper()
        if code in _ENTRIES:
            return _hit(code, "parsed_code")
        # Not a primary code — try it as an alias (handles legacy codes like PRA → PRM).
        found = _INDEX[0].get(normalize_track_name(code), set())
        if len(found) == 1:
            return _hit(next(iter(found)), "alias_exact")

    if track_name:
        keys = _name_keys(track_name)
        for level in (0, 1):
            found = _INDEX[level].get(keys[level], set())
            if len(found) == 1:
                return _hit(next(iter(found)), "alias_exact")
            if len(found) > 1:
                return {**_UNRESOLVED, "resolution_source": "ambiguous"}
        bare = track_name.strip().upper()
        if bare in _ENTRIES:
            return _hit(bare, "parsed_code")
        found = _INDEX[2].get(keys[2], set())
        if len(found) == 1:
            return _hit(next(iter(found)), "alias_normalized")
        if len(found) > 1:
            return {**_UNRESOLVED, "resolution_source": "ambiguous"}
        matches = get_close_matches(keys[0], list(_FUZZY_KEYS), n=1, cutoff=0.85)
        if matches:
            return _hit(_FUZZY_KEYS[matches[0]], "alias_fuzzy")

    return dict(_UNRESOLVED)
