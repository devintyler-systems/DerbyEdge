"""Track registry: Equibase abbreviation list + DraftKings spellings -> one canonical code."""
from __future__ import annotations

import csv
import dataclasses
import glob
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.derbyedge import tracks as T
from src.derbyedge.tracks import is_race_venue, resolve_track, track_timezone
from src.ingest.draftkings_markdown import parse_draftkings_markdown, validate_draftkings_markdown_card
from src.services.draftkings_markdown_intake import persist_validated_draftkings_markdown

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "reference"
AS_OF = datetime(2026, 10, 9, tzinfo=timezone.utc)
VALID_KINDS = {"RACETRACK", "FAIR", "FARM", "TRAINING"}

# Track strings DraftKings prints that no registry file can resolve.  Empty now that the
# operator-supplied codes are in track_additions.csv; a new name must be added there.
KNOWN_GAPS: set[str] = set()


def _rows(name):
    with (REF / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_every_equibase_row_is_loaded_with_a_valid_kind():
    rows = _rows("equibase_track_abbreviations.csv")
    assert len(rows) == 342
    codes = [r["code"] for r in rows]
    assert len(set(codes)) == len(codes)
    assert {r["kind"] for r in rows} <= VALID_KINDS
    for r in rows:
        assert resolve_track(track_code=r["code"])["track_code"] == r["code"], r
    extra = _rows("track_additions.csv")
    assert len(extra) == 5 and not ({r["code"] for r in extra} & set(codes))
    assert T.registry_size() >= 342 + len(extra) + 1       # + PRX (curated, absent from the Equibase list) + new sources


def test_every_curated_track_has_a_timezone_and_every_zone_is_valid():
    for rec in T._TRACKS:
        assert track_timezone(rec.code), rec.code
    for row in _rows("track_timezones.csv"):
        ZoneInfo(row["timezone"])


@pytest.mark.parametrize("text,code", [
    ("BELMONT AT THE BIG A", "BAQ"), ("Belmont at the Big A", "BAQ"), ("BAQ", "BAQ"),
    ("BEL", "BEL"), ("Belmont Park", "BEL"), ("Belmont", "BEL"),
    ("SAR", "SAR"), ("Saratoga", "SAR"), ("SARATOGA RACE COURSE", "SAR"),
    ("CD", "CD"), ("Churchill Downs", "CD"), ("AQU", "AQU"), ("Aqueduct Racetrack", "AQU"),
    ("Santa Anita", "SA"), ("SA", "SA"), ("DMR", "DMR"), ("Del Mar", "DMR"),
    ("Mountaineer Park", "MNR"), ("Mountaineer Casino Racetrack & Resort", "MNR"),
    ("Hollywood Casino at Charles Town Races", "CT"), ("Charles Town", "CT"), ("CT", "CT"),
    ("Los Alamitos Race Course", "LA"), ("Zia Park", "ZIA"), ("Penn National", "PEN"),
    ("PEN", "PEN"), ("Pimlico Race Course", "PIM"), ("Hawthorne", "HAW"),
    ("Horseshoe Indianapolis", "IND"), ("Indiana Downs", "IND"), ("Parx", "PRX"), ("PRX", "PRX"),
    ("Tampa Bay Downs", "TAM"), ("Kentucky Downs", "KD"), ("Lone Star Park", "LS"),
])
def test_the_acceptance_aliases_resolve_to_one_code(text, code):
    assert resolve_track(track_name=text)["track_code"] == code


@pytest.mark.parametrize("text,code,kind", [
    ("PALM MEADOWS TRAINING CENTER", "PMM", "TRAINING"),
    ("THE THOROUGHBRED CENTER", "TTC", "TRAINING"),
    ("NELSON JONES FARMS AND TRAINING CENTER, INC.", "NJF", "FARM"),
    ("HIGHPOINT FARM AND TRAINING CENTER", "HPT", "TRAINING"),
    ("OAKRIDGE TRAINING CENTER", "OEC", "TRAINING"),
    ("MCKATHAN BROTHERS FARM", "MBF", "FARM"),
    ("Brown County Fair", "BCF", "FAIR"),
])
def test_dk_long_spellings_of_training_centres_and_farms_resolve(text, code, kind):
    r = resolve_track(track_name=text)
    assert (r["track_code"], r["kind"]) == (code, kind)


@pytest.mark.parametrize("text,code,kind", [
    ("BELTERRA PARK", "BTP", "RACETRACK"), ("MAHONING VALLEY RACE COURSE", "MVR", "RACETRACK"),
    ("WINSTAR TRAINING CENTER", "WSR", "TRAINING"), ("LYNWOOD STABLE,INC", "LYN", "FARM"),
    ("BOLO FARM", "BLF", "FARM"), ("BTP", "BTP", "RACETRACK"),
])
def test_operator_supplied_tracks_resolve(text, code, kind):
    r = resolve_track(track_name=text)
    assert (r["track_code"], r["kind"]) == (code, kind)
    assert track_timezone("BTP") == track_timezone("MVR") == "America/New_York"


def test_only_racetracks_and_fairs_are_race_venues():
    assert is_race_venue("SAR") and is_race_venue("ZIA") and is_race_venue("BCF") and is_race_venue("BTP")
    assert not is_race_venue("PMM") and not is_race_venue("NJF") and not is_race_venue("WSR") and not is_race_venue("NOPE")


def test_ambiguous_and_unknown_names_are_never_guessed():
    assert resolve_track(track_name="Eclipse") == {
        "track_code": None, "track_name_canonical": None, "resolution_source": "ambiguous", "kind": None,
    }
    for name in ("Zzyzx Downs", "", None):
        assert resolve_track(track_name=name)["track_code"] is None


def test_the_pdf_scan_dictionaries_are_not_widened_by_the_full_registry():
    curated = {rec.code for rec in T._TRACKS}
    assert set(T.TRACK_CODES_UPPER.values()) <= curated
    assert "ELY" not in T.TRACK_CODES_UPPER and "PEG" not in T.TRACK_CODES_UPPER


def test_every_track_string_in_the_fixtures_resolves_except_the_known_gaps():
    strings = set()
    paths = glob.glob(str(ROOT / "tests" / "fixtures" / "*.md")) + glob.glob(
        str(ROOT / "draftkings_racedata_pdfs" / "fixtures" / "*DK_Horse*.md"))
    for path in paths:
        if "Speed" in path:
            continue
        card = parse_draftkings_markdown(Path(path).read_text(encoding="utf-8"),
                                         source_path=Path(path).name, as_of=AS_OF)
        strings.add(card.race.track)
        strings |= {p.track for p in card.past_performances} | {w.track for w in card.workouts}
    unresolved = {s for s in strings if s and not resolve_track(track_name=s)["track_code"]}
    assert unresolved <= KNOWN_GAPS, f"unregistered track strings: {sorted(unresolved - KNOWN_GAPS)}"


def _sar_with_header(header: str):
    raw = (ROOT / "tests" / "fixtures" / "SAR_DK_Horse_R6_9-4-26.md").read_text(encoding="utf-8")
    assert raw.startswith("Saratoga")
    return parse_draftkings_markdown(header + raw[len("Saratoga"):], source_path="SAR_DK_Horse_R6_9-4-26.md", as_of=AS_OF)


def test_an_unregistered_race_track_fails_validation_with_the_reason():
    v = validate_draftkings_markdown_card(_sar_with_header("Zzyzx Downs"))
    assert not v.passed
    assert any("'Zzyzx Downs' is not in the track registry" in e for e in v.errors)


def test_a_farm_cannot_be_the_race_track():
    v = validate_draftkings_markdown_card(_sar_with_header("Abracadabra Farm"))
    assert any("not a race venue" in e for e in v.errors)


def test_persist_refuses_to_invent_a_code_for_an_unknown_track():
    card = _sar_with_header("Zzyzx Downs")
    forced = dataclasses.replace(validate_draftkings_markdown_card(card), errors=[], passed=True)
    conn = sqlite3.connect(":memory:")
    conn.executescript("\n".join(
        l for l in (ROOT / "db" / "schema.sql").read_text(encoding="utf-8").splitlines() if "journal_mode" not in l))
    with pytest.raises(ValueError, match="unrecognized track"):
        persist_validated_draftkings_markdown(conn, card, forced, source_filename="x_R6_9-4-26.md")
    assert conn.execute("SELECT COUNT(*) FROM tracks WHERE abbrev LIKE 'ZZY%'").fetchone()[0] == 0


def test_target_track_record_uses_the_real_code_not_the_first_four_letters():
    raw = (ROOT / "tests" / "fixtures" / "CT_Full_Race_Data_R7_10-8-26.md").read_text(encoding="utf-8")
    sections_start = raw.index("Charles Town")
    card = parse_draftkings_markdown(raw, source_path="CT_Full_Race_Data_R7_10-8-26.md", as_of=AS_OF)
    assert sections_start == 0
    assert card.entries[0].record_splits["target_track"].starts == 8     # "CT 8 1 1 1 $35,228"


# ---- the track listing and the Equineline directory ------------------------------------------

def test_both_extracts_are_present_and_every_code_is_in_the_registry():
    listing = _rows("source_listing_tracks.csv")
    equineline = _rows("source_equineline_tracks.csv")
    assert len(listing) == 337 and len(equineline) == 370
    for row in listing + equineline:
        assert T.get_track(row["code"]) is not None, row["code"]
    assert T.registry_size() >= 600


def test_adding_sources_did_not_change_what_any_primary_name_resolves_to():
    """Every curated / Equibase / addition / operator-alias spelling still lands on its own code."""
    expected = {}
    for rec in T._TRACKS:
        for name in (rec.name, *rec.aliases):
            expected[name] = rec.code
    for fname in ("equibase_track_abbreviations.csv", "track_additions.csv"):
        for r in _rows(fname):
            expected.setdefault(r["name"], r["code"])
    for r in _rows("track_aliases.csv"):
        expected[r["alias"]] = r["code"]
    wrong = {n: (c, resolve_track(track_name=n)["track_code"]) for n, c in expected.items()
             if resolve_track(track_name=n)["track_code"] != c}
    assert not wrong, wrong


def test_belmont_at_the_big_a_and_belmont_park_are_different_tracks():
    for name in ("Belmont At The Big A", "BELMONT AT THE BIG A", "Belmont Park at the Big A"):
        assert resolve_track(track_name=name)["track_code"] == "BAQ", name
    for name in ("Belmont Park", "Belmont", "BEL"):
        assert resolve_track(track_name=name)["track_code"] == "BEL", name
    assert T.get_track("BAQ")["state"] == "NY" and track_timezone("BAQ") == "America/New_York"
    assert is_race_venue("BAQ") and is_race_venue("BEL")


def test_historic_tracks_from_horse_histories_resolve():
    for name, code in [("Bowie", "BOW"), ("Arlington Park", "AP"), ("Hollywood Park", "HOL"),
                       ("Calder Race Course", "CRC"), ("Bay Meadows", "BM"), ("Agua Caliente", "AC")]:
        assert resolve_track(track_name=name)["track_code"] == code, name


def test_two_tracks_sharing_a_name_are_ambiguous_not_first_come():
    assert resolve_track(track_name="Arizona Downs")["resolution_source"] == "ambiguous"
    assert resolve_track(track_code="AD")["track_code"] == "AD" and resolve_track(track_code="AZD")["track_code"] == "AZD"


def test_wager_and_special_pseudo_tracks_are_never_race_venues():
    for code in ("CCP", "DBA", "BCA", "EQA"):
        assert T.get_track(code)["kind"] == "OTHER" and not is_race_venue(code)
    assert resolve_track(track_name="Cross Country Pick Four")["kind"] == "OTHER"


def test_state_location_and_renaming_notes_are_kept():
    sar = T.get_track("SAR")
    assert sar["state"] == "NY" and "SARATOGA" in sar["location"].upper()
    assert "Arlington Park" in T.get_track("AP")["note"]
    assert "Kentucky Downs" in T.get_track("DUE")["note"]


def test_derived_timezones_require_state_and_offset_to_agree():
    derived = {r["code"]: r["timezone"] for r in _rows("track_timezones_derived.csv")}
    assert derived["ATL"] == "America/New_York" and derived["ALB"] == "America/Denver"   # NJ / 3, NM / 1
    assert derived["AZD"] == "America/Phoenix"                                            # AZ does not observe DST
    # listing says offset 0 for these; the state says otherwise, so no zone is invented
    for code in ("ARP", "BOI"):
        assert code not in derived
    # the hand-maintained file wins over derivation
    assert track_timezone("CD") == "America/New_York" and track_timezone("DMR") == "America/Los_Angeles"
    for zone in derived.values():
        ZoneInfo(zone)
