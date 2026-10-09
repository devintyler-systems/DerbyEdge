"""TwinSpires RACE STATS block: pars, race-type stats, track bias and post bias (meet and week).

The block is pasted as one cell per line.  This parser is strict by design: every table's column labels must match the
sample (``tests/fixtures/TS_RaceStats_BEL_R5_10-9-26.md``) literally and every value must be a well-formed number, so a
layout change fails with the offending text named instead of silently shifting values into the wrong column.

Nothing here feeds scoring.  The block is captured now because it cannot be reconstructed as-of later.  Format knowledge
rests on one real sample (UNVERIFIED for other tracks, surfaces, claiming vs. stakes races, or empty bias samples).
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from typing import Any

PARSER_VERSION = "twinspires_race_stats_v1"
HEADING = "RACE STATS"

_RACE_TYPE_YEARS = re.compile(r"^Race Type Stats for Last (\d+) Years?$", re.I)
_BIAS_HEADING = re.compile(
    r"^(?P<surface>[A-Za-z ]+?) (?P<distance>\d+(?:\.\d+)?f) Track Bias Stats: (?P<scope>[A-Za-z]+) "
    r"\((?P<start>\d{2}/\d{2}) - (?P<end>\d{2}/\d{2})\)$"
)
_PAR_PAIR = re.compile(r"([A-Z][A-Z0-9]*):\s*(-?\d+)")

_RT_LABELS_1 = ["Race Type", "# Races", "FAV Win%", "FAV ITM Win%", "FAV $2 ROI"]
_RT_LABELS_2 = ["Average Field Size", "Median $2 Win Payoff", "% Winners <5/1", "% Winners >=5/1<10/1", "% Winners >=10/1"]
_BIAS_LABELS = ["# Races", "% Wire", "Speed Bias", "WNR Avg BL", "1stCall", "2ndCall"]
_RUN_STYLE_LABELS = ["Early Speed", "Late Speed", "E", "E/P", "P", "S"]    # the first two group the four style columns
_RUN_STYLES = ["E", "E/P", "P", "S"]
_POST_LABELS = ["RAIL", "1-3", "4-7", "8+"]


class RaceStatsError(ValueError):
    """The RACE STATS block is present but malformed; the message names what was expected and what was found."""


@dataclasses.dataclass
class RaceTypeStats:
    race_type: str
    races: int
    fav_win_pct: float
    fav_itm_win_pct: float
    fav_roi_2: float
    avg_field_size: float
    median_win_payoff: float
    pct_winners_under_5_1: float
    pct_winners_5_1_to_10_1: float
    pct_winners_10_1_plus: float


@dataclasses.dataclass
class TrackBias:
    scope: str                      # "Meet" / "Week", as printed
    surface: str
    distance: str                   # as printed, e.g. "8.5f"
    period_start: str               # MM/DD as printed (no year in the source)
    period_end: str
    races: int                      # the sample size behind every figure below
    wire_pct: float
    speed_bias_pct: float
    wnr_avg_bl_1st_call: float
    wnr_avg_bl_2nd_call: float
    run_style_impact: dict[str, float]
    run_style_pct_won: dict[str, float]
    post_impact: dict[str, float]
    post_avg_win_pct: dict[str, float]


@dataclasses.dataclass
class RaceStats:
    track: str
    header_lines: list[str]         # the descriptive lines above PARS, as printed (leading "| " removed)
    pars: dict[str, int]
    race_type_years: int
    race_types: list[RaceTypeStats]
    track_bias: list[TrackBias]
    raw_text: str
    raw_sha256: str
    parser_version: str = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        out.pop("raw_text")             # stored beside the parsed form, not inside it
        return out


class _Tokens:
    def __init__(self, tokens: list[str]) -> None:
        self.t, self.i = tokens, 0

    def done(self) -> bool:
        return self.i >= len(self.t)

    def peek(self) -> str:
        return self.t[self.i] if not self.done() else ""

    def take(self, n: int, what: str) -> list[str]:
        if self.i + n > len(self.t):
            raise RaceStatsError(f"{what}: expected {n} more line(s) but the block ends")
        out = self.t[self.i:self.i + n]
        self.i += n
        return out

    def labels(self, expected: list[str], what: str) -> None:
        got = self.take(len(expected), what)
        if got != expected:
            raise RaceStatsError(f"{what}: expected the column labels {expected} but found {got}")


def _num(token: str, kind: str, what: str) -> float:
    pattern = {"pct": r"-?\d+(?:\.\d+)?%", "money": r"\$-?\d+(?:,\d{3})*(?:\.\d+)?", "float": r"-?\d+(?:\.\d+)?",
               "int": r"\d+"}[kind]
    if not re.fullmatch(pattern, token):
        raise RaceStatsError(f"{what}: {token!r} is not a valid {kind} value")
    return float(token.rstrip("%").lstrip("$").replace(",", ""))


def _values(tok: _Tokens, kind: str, count: int, what: str) -> list[float]:
    return [_num(v, kind, what) for v in tok.take(count, what)]


def _parse_header(tok: _Tokens) -> tuple[str, list[str], dict[str, int]]:
    head: list[str] = []
    while not tok.done() and not _RACE_TYPE_YEARS.match(tok.peek()):
        head.append(tok.take(1, "header")[0])
    if tok.done():
        raise RaceStatsError("header: no 'Race Type Stats for Last N Years' line found")
    if not head:
        raise RaceStatsError("header: no track / race description lines before the stats tables")
    par_lines = [h for h in head if "PARS:" in h]
    if len(par_lines) != 1:
        raise RaceStatsError(f"header: expected exactly one PARS line, found {len(par_lines)}")
    par_text = par_lines[0].split("PARS:", 1)[1]
    pairs = _PAR_PAIR.findall(par_text)
    if not pairs or re.sub(r"[|\s]+", "", _PAR_PAIR.sub("", par_text)):
        raise RaceStatsError(f"header: PARS line is not 'KEY: integer' pairs: {par_lines[0]!r}")
    pars = {k: int(v) for k, v in pairs}
    if len(pars) != len(pairs):
        raise RaceStatsError(f"header: duplicate PARS key in {par_lines[0]!r}")
    track = head[0].strip()
    if track.startswith("|"):
        raise RaceStatsError(f"header: first line should be the track code, found {head[0]!r}")
    lines = [h.lstrip("| ").rstrip() for h in head[1:] if "PARS:" not in h]
    return track, lines, pars


def _parse_race_types(tok: _Tokens) -> list[RaceTypeStats]:
    tok.labels(_RT_LABELS_1, "race type table")
    first_end = next((k for k in range(tok.i, len(tok.t)) if tok.t[k] == _RT_LABELS_2[0]), None)
    if first_end is None:
        raise RaceStatsError(f"race type table: no {_RT_LABELS_2[0]!r} table follows it")
    width = len(_RT_LABELS_1)
    n_cells = first_end - tok.i
    if n_cells == 0 or n_cells % width:
        raise RaceStatsError(f"race type table: {n_cells} cell(s) is not a whole number of {width}-column rows")
    rows1 = [tok.take(width, "race type table") for _ in range(n_cells // width)]
    tok.labels(_RT_LABELS_2, "race type payoff table")
    rows2 = []
    for _ in rows1:
        rows2.append(tok.take(len(_RT_LABELS_2), "race type payoff table"))
    out = []
    for n, (a, b) in enumerate(zip(rows1, rows2), start=1):
        what = f"race type row {n} ({a[0]!r})"
        out.append(RaceTypeStats(
            race_type=a[0], races=int(_num(a[1], "int", what)), fav_win_pct=_num(a[2], "pct", what),
            fav_itm_win_pct=_num(a[3], "pct", what), fav_roi_2=_num(a[4], "float", what),
            avg_field_size=_num(b[0], "float", what), median_win_payoff=_num(b[1], "money", what),
            pct_winners_under_5_1=_num(b[2], "pct", what), pct_winners_5_1_to_10_1=_num(b[3], "pct", what),
            pct_winners_10_1_plus=_num(b[4], "pct", what)))
    return out


def _parse_bias(tok: _Tokens, heading: re.Match) -> TrackBias:
    what = f"track bias '{heading.group('scope')}'"
    tok.labels(_BIAS_LABELS, what)
    races, wire, speed, bl1, bl2 = (_values(tok, "int", 1, what + " # Races")[0], *_values(tok, "pct", 2, what),
                                    *_values(tok, "float", 2, what))
    tok.labels(["Run Style", *_RUN_STYLE_LABELS], what + " run style")
    tok.labels(["Impact Values"], what + " run style")
    style_impact = _values(tok, "float", len(_RUN_STYLES), what + " run style impact")
    tok.labels(["% Races Won"], what + " run style")
    style_won = _values(tok, "pct", len(_RUN_STYLES), what + " run style % won")
    tok.labels(["Post Bias", *_POST_LABELS], what + " post bias")
    tok.labels(["Impact Values"], what + " post bias")
    post_impact = _values(tok, "float", len(_POST_LABELS), what + " post impact")
    tok.labels(["AVG Win %"], what + " post bias")
    post_win = _values(tok, "pct", len(_POST_LABELS), what + " post avg win %")
    return TrackBias(
        scope=heading.group("scope"), surface=heading.group("surface"), distance=heading.group("distance"),
        period_start=heading.group("start"), period_end=heading.group("end"), races=int(races), wire_pct=wire,
        speed_bias_pct=speed, wnr_avg_bl_1st_call=bl1, wnr_avg_bl_2nd_call=bl2,
        run_style_impact=dict(zip(_RUN_STYLES, style_impact)), run_style_pct_won=dict(zip(_RUN_STYLES, style_won)),
        post_impact=dict(zip(_POST_LABELS, post_impact)), post_avg_win_pct=dict(zip(_POST_LABELS, post_win)))


def parse_race_stats(text: str) -> RaceStats:
    """Parse one RACE STATS block; raise ``RaceStatsError`` naming the problem if it is not exactly the known layout."""
    tokens = [ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if not tokens or tokens[0] != HEADING:
        raise RaceStatsError(f"block must start with the line {HEADING!r}")
    tok = _Tokens(tokens[1:])
    track, header_lines, pars = _parse_header(tok)
    years = int(_RACE_TYPE_YEARS.match(tok.take(1, "race type heading")[0]).group(1))   # type: ignore[union-attr]
    race_types = _parse_race_types(tok)
    bias: list[TrackBias] = []
    while not tok.done():
        heading = _BIAS_HEADING.match(tok.peek())
        if heading is None:
            raise RaceStatsError(f"unrecognised line where a track bias heading was expected: {tok.peek()!r}")
        tok.take(1, "track bias heading")
        parsed = _parse_bias(tok, heading)
        if any(b.scope == parsed.scope for b in bias):
            raise RaceStatsError(f"track bias '{parsed.scope}' appears twice")
        bias.append(parsed)
    raw = "\n".join(text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n")) + "\n"
    return RaceStats(
        track=track, header_lines=header_lines, pars=pars, race_type_years=years, race_types=race_types,
        track_bias=bias, raw_text=raw, raw_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest())
