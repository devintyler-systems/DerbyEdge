"""Extract the two track-code PDFs into data/reference/ CSVs.

    python scripts/extract_track_sources.py --listing <Thoroughbred_Quarter_Horse_Track_Listing.pdf> \
                                            --equineline <equineline_Directory_of_Reference.pdf>

* listing    -> source_listing_tracks.csv     code, proper_case, state, tz_offset_from_pacific, name
* equineline -> source_equineline_tracks.csv  code, results_charts, name, location, note

The PDFs themselves are not committed (the repo ignores *.pdf); the CSVs are the reviewed extract.
"""
from __future__ import annotations

import argparse
import collections
import csv
import re
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "reference"
_LISTING_ROW = re.compile(
    r"^(?P<code>[A-Z][A-Z0-9]{1,3})\s+(?P<proper>\S+)\s+(?:(?P<state>[A-Z]{2,3})\s+)?(?P<off>\d)\s+(?P<name>\S.*)$"
)


def extract_listing(path: Path) -> list[dict]:
    rows: list[dict] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                line = line.strip()
                match = _LISTING_ROW.match(line)
                if match:
                    d = match.groupdict()
                    rows.append({
                        "code": d["code"], "proper_case": d["proper"], "state": d["state"] or "",
                        "tz_offset_from_pacific": d["off"], "name": d["name"].strip(),
                    })
                elif line and not line.startswith(("Listing of", "Click on", "Track Code")):
                    print(f"listing: unparsed line {line!r}", file=sys.stderr)
    return rows


def extract_equineline(path: Path) -> list[dict]:
    rows: list[dict] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            by_line = collections.defaultdict(list)
            for w in page.extract_words():
                by_line[round(w["top"])].append(w)
            tops = sorted(by_line)
            merged: list[list] = []
            for top in tops:
                if merged and abs(top - merged[-1][0]) <= 3:
                    merged[-1][1].extend(by_line[top])
                else:
                    merged.append([top, list(by_line[top])])
            for _top, words in merged:
                words.sort(key=lambda w: w["x0"])
                code = " ".join(w["text"] for w in words if w["x0"] < 60)
                flag = " ".join(w["text"] for w in words if 60 <= w["x0"] < 110)
                name = " ".join(w["text"] for w in words if 110 <= w["x0"] < 325)
                loc = " ".join(w["text"] for w in words if w["x0"] >= 325)
                text = " ".join(w["text"] for w in words)
                if text.startswith("(") or (rows and rows[-1]["note"] and not rows[-1]["note"].endswith(")")
                                            and not re.fullmatch(r"[A-Z][A-Z0-9]{1,3}", code)):
                    if rows and (text.startswith("(") or rows[-1]["note"]):
                        rows[-1]["note"] = (rows[-1]["note"] + " " + text).strip()
                        continue
                if re.fullmatch(r"[A-Z][A-Z0-9]{1,3}", code) and name:
                    rows.append({"code": code, "results_charts": flag, "name": name, "location": loc, "note": ""})
                # everything else is page furniture (menu bar, title, intro paragraph, column headers, copyright)
    return rows


# ---------------------------------------------------------------------------
# Timezones.  The listing's "offset from Pacific" is unreliable on its own (0 is also its
# placeholder: Arapahoe Park CO and Les Bois ID both show 0).  A zone is therefore derived
# only when the state and the offset AGREE; everything else is left blank (the engine then
# reports "no timezone" rather than computing a wrong post time).
# ---------------------------------------------------------------------------
_E, _C, _M, _P = "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"
_SINGLE = {
    **{s: (_E, 3) for s in "CT DE GA MA MD ME NC NH NJ NY OH PA RI SC VA VT WV DC".split()},
    **{s: (_C, 2) for s in "AL AR IL IA LA MN MO MS OK WI".split()},
    **{s: (_M, 1) for s in "CO MT NM UT WY".split()},
    **{s: (_P, 0) for s in "CA WA".split()},
    "AZ": ("America/Phoenix", 1), "PR": ("America/Puerto_Rico", 3),
}
_SPLIT = {      # state -> {offset: zone}; offset 0 carries no information for these
    "KY": {3: _E, 2: _C}, "TN": {3: _E, 2: _C}, "FL": {3: _E, 2: _C}, "MI": {3: _E, 2: _C},
    "IN": {3: "America/Indiana/Indianapolis", 2: _C},
    "TX": {2: _C, 1: _M}, "KS": {2: _C, 1: _M}, "NE": {2: _C, 1: _M}, "ND": {2: _C, 1: _M}, "SD": {2: _C, 1: _M},
    "ID": {1: _M}, "OR": {0: _P, 1: "America/Boise"}, "NV": {0: _P},
}


def derive_timezones(listing: list[dict]) -> tuple[list[dict], list[dict]]:
    derived, skipped = [], []
    for row in listing:
        state, off = row["state"], int(row["tz_offset_from_pacific"])
        zone = None
        if state in _SINGLE and _SINGLE[state][1] == off:
            zone, basis = _SINGLE[state][0], f"state {state} and listing offset {off} agree"
        elif state in _SPLIT and off in _SPLIT[state] and not (off == 0 and state not in ("OR", "NV")):
            zone, basis = _SPLIT[state][off], f"split state {state}; listing offset {off}"
        if zone:
            derived.append({"code": row["code"], "timezone": zone, "basis": basis})
        elif state in _SINGLE or state in _SPLIT:
            skipped.append({"code": row["code"], "name": row["name"], "state": state, "offset": off})
    return derived, skipped


def write(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for row in sorted(rows, key=lambda r: r["code"]):
            w.writerow({k: row[k].replace("’", "'") for k in fields})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listing", type=Path, required=True)
    ap.add_argument("--equineline", type=Path, required=True)
    args = ap.parse_args()
    listing = extract_listing(args.listing)
    equineline = extract_equineline(args.equineline)
    write(OUT / "source_listing_tracks.csv", listing, ["code", "proper_case", "state", "tz_offset_from_pacific", "name"])
    write(OUT / "source_equineline_tracks.csv", equineline, ["code", "results_charts", "name", "location", "note"])
    derived, skipped = derive_timezones(listing)
    write(OUT / "track_timezones_derived.csv", derived, ["code", "timezone", "basis"])
    print(f"timezones derived={len(derived)}; state/offset disagreements left blank={len(skipped)}:")
    for row in skipped:
        print(f"   {row['code']:4} {row['state']} offset={row['offset']} {row['name']}")
    print(f"listing rows={len(listing)} unique codes={len({r['code'] for r in listing})}")
    print(f"equineline rows={len(equineline)} unique codes={len({r['code'] for r in equineline})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
