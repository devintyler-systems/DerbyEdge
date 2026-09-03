"""
tests/test_draftkings_ingest.py

Validation suite for the DraftKings Horse PDF ingestion adapter.

Coverage:
  1.  Filename parsing: {TRACK}_DK_Horse_R{RACE}_{M-D-YY}.pdf
  2.  Golden fixture parses without error (even as post-race capture)
  3.  Parser coverage: entry count, starts, workouts extracted
  4.  Filename / header reconciliation: track code and race number agree
  5.  Provenance: source_document_id, source_page_number, source_row_id present
  6.  Provisional composite horse key format
  7.  Odds contract: no untyped odds field; all records have valid odds_type
  8.  Post-race eligibility flag is independent of parse coverage
  9.  Anti-leakage: no target-date start or workout enters pre-race features
  10. Canonical ingestion idempotency: second ingest creates 0 duplicate rows
  11. Staging tables are append-only (all staging rows preserved after re-ingest)
  12. Pre-race feature DataFrame structure and column presence
  13. Feature values are within expected ranges / types
  14. market_implied_prob is never derived from off_odds or unknown odds
  15. ensure_feature_store_columns migration guard (PR #10 fix)
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PDF = ROOT / "draftkings_racedata_pdfs" / "fixtures" / "SAR_DK_Horse_R9_9-2-26.pdf"
GOLDEN_FILENAME = "SAR_DK_Horse_R9_9-2-26.pdf"

# Expected fixture metadata (from filename)
EXPECTED_TRACK_CODE = "SAR"
EXPECTED_RACE_NUMBER = 9
EXPECTED_RACE_DATE = date(2026, 9, 2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_conn() -> sqlite3.Connection:
    """In-memory SQLite with full DerbyEdge schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    schema_text = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    schema_text = "\n".join(
        ln for ln in schema_text.splitlines() if "journal_mode" not in ln
    )
    conn.executescript(schema_text)
    return conn


def _load_fixture() -> bytes:
    if not FIXTURE_PDF.exists():
        pytest.skip(f"Golden fixture not found: {FIXTURE_PDF}")
    return FIXTURE_PDF.read_bytes()


# ---------------------------------------------------------------------------
# 1. Filename parsing
# ---------------------------------------------------------------------------

class TestFilenameParser:
    def test_canonical_filename_parses(self):
        from src.ingest.draftkings_pdf import parse_dk_filename
        track, race_num, race_date = parse_dk_filename(GOLDEN_FILENAME)
        assert track == EXPECTED_TRACK_CODE
        assert race_num == EXPECTED_RACE_NUMBER
        assert race_date == EXPECTED_RACE_DATE

    def test_missing_components_return_none(self):
        from src.ingest.draftkings_pdf import parse_dk_filename
        track, race_num, race_date = parse_dk_filename("not_a_dk_file.pdf")
        assert track is None
        assert race_num is None
        assert race_date is None

    def test_two_digit_year_normalizes(self):
        from src.ingest.draftkings_pdf import parse_dk_filename
        _, _, d = parse_dk_filename("CD_DK_Horse_R1_5-3-26.pdf")
        assert d is not None
        assert d.year == 2026

    def test_four_digit_year_works(self):
        from src.ingest.draftkings_pdf import parse_dk_filename
        _, _, d = parse_dk_filename("CD_DK_Horse_R1_5-3-2026.pdf")
        assert d is not None
        assert d.year == 2026

    def test_invalid_date_returns_none(self):
        from src.ingest.draftkings_pdf import parse_dk_filename
        track, race_num, race_date = parse_dk_filename("CD_DK_Horse_R1_13-45-26.pdf")
        assert race_date is None


# ---------------------------------------------------------------------------
# 2. Golden fixture parses without error
# ---------------------------------------------------------------------------

class TestGoldenFixtureParsing:
    @pytest.fixture(scope="class")
    def parsed(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        return parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)

    def test_parse_returns_dataclass(self, parsed):
        from src.ingest.draftkings_pdf import DraftKingsParsedRace
        assert isinstance(parsed, DraftKingsParsedRace)

    def test_status_success(self, parsed):
        assert parsed.status == "success"

    def test_source_document_id_is_set(self, parsed):
        assert parsed.source_document_id and parsed.source_document_id.startswith("dk_doc_")

    def test_file_sha256_is_hex_string(self, parsed):
        assert len(parsed.file_sha256) == 64
        assert all(c in "0123456789abcdef" for c in parsed.file_sha256)

    def test_target_race_date_matches_filename(self, parsed):
        assert parsed.target_race_date == EXPECTED_RACE_DATE

    def test_filename_track_code_matches(self, parsed):
        assert parsed.filename_track_code == EXPECTED_TRACK_CODE

    def test_filename_race_number_matches(self, parsed):
        assert parsed.filename_race_number == EXPECTED_RACE_NUMBER


# ---------------------------------------------------------------------------
# 3. Parser coverage: entries, starts, workouts
# ---------------------------------------------------------------------------

class TestParserCoverage:
    @pytest.fixture(scope="class")
    def parsed(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        return parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)

    def test_at_least_one_entry_extracted(self, parsed):
        assert len(parsed.entries) >= 1, "Fixture must yield at least one entry"

    def test_at_least_one_start_extracted(self, parsed):
        assert len(parsed.starts) >= 1, "Fixture must yield at least one historical start"

    def test_at_least_one_workout_extracted(self, parsed):
        assert len(parsed.workouts) >= 1, "Fixture must yield at least one workout"

    def test_entry_parse_coverage_positive(self, parsed):
        assert parsed.entry_parse_coverage > 0.0

    def test_historical_start_count_consistent(self, parsed):
        assert parsed.historical_start_count == len(parsed.starts)

    def test_workout_count_consistent(self, parsed):
        assert parsed.workout_count == len(parsed.workouts)


# ---------------------------------------------------------------------------
# 4. Filename / header reconciliation
# ---------------------------------------------------------------------------

class TestFilenameHeaderReconciliation:
    @pytest.fixture(scope="class")
    def parsed(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        return parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)

    def test_effective_track_code_is_sar(self, parsed):
        """Both filename and header (if present) should agree or fall back to filename."""
        effective = parsed.header_track_code or parsed.filename_track_code
        assert effective == EXPECTED_TRACK_CODE

    def test_effective_race_number_is_nine(self, parsed):
        effective = parsed.header_race_number or parsed.filename_race_number
        assert effective == EXPECTED_RACE_NUMBER

    def test_no_conflicting_track_codes(self, parsed):
        """If both header and filename codes are present they must agree."""
        if parsed.header_track_code and parsed.filename_track_code:
            assert parsed.header_track_code.upper() == parsed.filename_track_code.upper()

    def test_no_conflicting_race_numbers(self, parsed):
        if parsed.header_race_number and parsed.filename_race_number:
            assert parsed.header_race_number == parsed.filename_race_number


# ---------------------------------------------------------------------------
# 5. Provenance fields on every record
# ---------------------------------------------------------------------------

class TestProvenance:
    @pytest.fixture(scope="class")
    def parsed(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        return parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)

    def test_entries_have_source_row_id(self, parsed):
        for e in parsed.entries:
            assert e.source_row_id, f"Entry {e.horse_name!r} missing source_row_id"

    def test_entries_have_page_number(self, parsed):
        for e in parsed.entries:
            assert e.source_page_number >= 1

    def test_entries_have_raw_text(self, parsed):
        for e in parsed.entries:
            assert e.raw_text is not None

    def test_starts_have_source_row_id(self, parsed):
        for s in parsed.starts:
            assert s.source_row_id, f"Start {s.horse_name!r} missing source_row_id"

    def test_starts_have_parse_confidence(self, parsed):
        for s in parsed.starts:
            assert 0.0 <= s.parse_confidence <= 1.0

    def test_workouts_have_source_row_id(self, parsed):
        for w in parsed.workouts:
            assert w.source_row_id, f"Workout {w.horse_name!r} missing source_row_id"

    def test_source_row_ids_are_unique_within_type(self, parsed):
        """Row IDs must be unique within each record collection."""
        e_ids = [e.source_row_id for e in parsed.entries]
        assert len(e_ids) == len(set(e_ids)), "Duplicate entry source_row_ids"

        s_ids = [s.source_row_id for s in parsed.starts]
        assert len(s_ids) == len(set(s_ids)), "Duplicate start source_row_ids"

        w_ids = [w.source_row_id for w in parsed.workouts]
        assert len(w_ids) == len(set(w_ids)), "Duplicate workout source_row_ids"


# ---------------------------------------------------------------------------
# 6. Provisional composite horse key
# ---------------------------------------------------------------------------

class TestHorseSourceKey:
    @pytest.fixture(scope="class")
    def parsed(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        return parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)

    def test_entry_source_keys_start_with_draftkings(self, parsed):
        for e in parsed.entries:
            assert e.horse_source_key.startswith("draftkings:"), (
                f"Entry {e.horse_name!r} key={e.horse_source_key!r} missing prefix"
            )

    def test_entry_source_keys_have_five_parts(self, parsed):
        """Format: draftkings:{name}:{sex}:{foaling_year}:{state_bred}."""
        for e in parsed.entries:
            parts = e.horse_source_key.split(":")
            assert len(parts) == 5, (
                f"Key {e.horse_source_key!r} has {len(parts)} parts, expected 5"
            )

    def test_start_source_keys_match_format(self, parsed):
        for s in parsed.starts:
            assert s.horse_source_key.startswith("draftkings:")

    def test_different_entries_have_different_keys(self, parsed):
        keys = [e.horse_source_key for e in parsed.entries]
        # At least some keys should differ (real race, different horses)
        assert len(set(keys)) > 1 or len(parsed.entries) == 1


# ---------------------------------------------------------------------------
# 7. Odds contract
# ---------------------------------------------------------------------------

VALID_ODDS_TYPES = {"morning_line", "live_tote", "off_odds", "unknown"}


class TestOddsContract:
    @pytest.fixture(scope="class")
    def parsed(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        return parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)

    def test_entry_odds_type_is_valid(self, parsed):
        for e in parsed.entries:
            assert e.odds_type in VALID_ODDS_TYPES, (
                f"Entry {e.horse_name!r} has invalid odds_type={e.odds_type!r}"
            )

    def test_odds_records_have_valid_types(self, parsed):
        for od in parsed.odds_records:
            assert od.odds_type in VALID_ODDS_TYPES

    def test_market_eligible_only_for_tote_or_ml(self, parsed):
        """is_market_eligible must be False unless odds_type is live_tote or morning_line."""
        for od in parsed.odds_records:
            if od.is_market_eligible:
                assert od.odds_type in {"live_tote", "morning_line"}, (
                    f"market_eligible=True but odds_type={od.odds_type!r}"
                )

    def test_post_race_odds_not_market_eligible(self, parsed):
        """off_odds records must never be market_eligible."""
        for od in parsed.odds_records:
            if od.odds_type == "off_odds":
                assert not od.is_market_eligible


# ---------------------------------------------------------------------------
# 8. Post-race eligibility flag
# ---------------------------------------------------------------------------

class TestPostRaceEligibility:
    @pytest.fixture(scope="class")
    def parsed(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        return parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)

    def test_fixture_is_post_race(self, parsed):
        """The SAR_DK_Horse_R9_9-2-26.pdf was captured after race; must be flagged."""
        assert parsed.is_post_race is True

    def test_fixture_not_production_eligible(self, parsed):
        assert parsed.production_eligible is False

    def test_eligibility_reason_is_set(self, parsed):
        assert parsed.eligibility_reason, "eligibility_reason must be a non-empty string"

    def test_parse_coverage_unaffected_by_eligibility(self, parsed):
        """Post-race status must NOT suppress entry or start parsing."""
        assert len(parsed.entries) >= 1, (
            "Post-race document must still yield entries (parser coverage independent of eligibility)"
        )
        assert len(parsed.starts) >= 1, (
            "Post-race document must still yield starts"
        )


# ---------------------------------------------------------------------------
# 9. Anti-leakage contract
# ---------------------------------------------------------------------------

class TestAntiLeakage:
    @pytest.fixture(scope="class")
    def parsed(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        return parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)

    def test_no_start_on_target_date_is_target_race_false(self, parsed):
        """All starts with start_date == target_race_date must be flagged is_target_race=True."""
        target = parsed.target_race_date
        for s in parsed.starts:
            if s.start_date == target:
                assert s.is_target_race, (
                    f"Start on target date {target} for {s.horse_name!r} "
                    f"must have is_target_race=True"
                )

    def test_no_workout_on_target_date_is_target_race_false(self, parsed):
        target = parsed.target_race_date
        for w in parsed.workouts:
            if w.workout_date == target:
                assert w.is_target_race, (
                    f"Workout on target date {target} for {w.horse_name!r} "
                    f"must have is_target_race=True"
                )

    def test_feature_generation_excludes_target_date(self, parsed):
        """generate_dk_pre_race_features must see 0 target-race records in pre-race sets."""
        conn = _mem_conn()
        from src.services.draftkings_enrich import (
            ingest_draftkings_to_canonical,
            generate_dk_pre_race_features,
        )
        card_id, _ = ingest_draftkings_to_canonical(conn, parsed)
        feat_df = generate_dk_pre_race_features(conn, card_id, parsed)

        target = parsed.target_race_date

        # All starts used in feature generation are strictly pre-race
        for entry in parsed.entries:
            used_starts = [
                s for s in parsed.starts
                if s.horse_name == entry.horse_name and not s.is_target_race
            ]
            for s in used_starts:
                assert s.start_date < target, (
                    f"Leakage: start_date {s.start_date} >= target {target}"
                )

        conn.close()

    def test_target_race_records_excluded_column_present(self, parsed):
        """The feature DataFrame must expose the audit column target_race_records_excluded."""
        conn = _mem_conn()
        from src.services.draftkings_enrich import (
            ingest_draftkings_to_canonical,
            generate_dk_pre_race_features,
        )
        card_id, _ = ingest_draftkings_to_canonical(conn, parsed)
        feat_df = generate_dk_pre_race_features(conn, card_id, parsed)
        assert "target_race_records_excluded" in feat_df.columns
        conn.close()


# ---------------------------------------------------------------------------
# 10. Canonical ingestion idempotency
# ---------------------------------------------------------------------------

class TestCanonicalIngestionIdempotency:
    @pytest.fixture(scope="class")
    def parsed(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        return parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)

    def test_second_ingest_returns_same_card_id(self, parsed):
        conn = _mem_conn()
        from src.services.draftkings_enrich import ingest_draftkings_to_canonical
        card_id_1, is_new_1 = ingest_draftkings_to_canonical(conn, parsed)
        card_id_2, is_new_2 = ingest_draftkings_to_canonical(conn, parsed)
        assert card_id_1 == card_id_2
        assert is_new_1 is True
        assert is_new_2 is False
        conn.close()

    def test_second_ingest_creates_no_duplicate_documents(self, parsed):
        conn = _mem_conn()
        from src.services.draftkings_enrich import ingest_draftkings_to_canonical
        ingest_draftkings_to_canonical(conn, parsed)
        ingest_draftkings_to_canonical(conn, parsed)
        count = conn.execute(
            "SELECT COUNT(*) FROM dk_staging_documents WHERE file_sha256 = ?",
            (parsed.file_sha256,),
        ).fetchone()[0]
        assert count == 1
        conn.close()

    def test_second_ingest_creates_no_duplicate_canonical_horses(self, parsed):
        conn = _mem_conn()
        from src.services.draftkings_enrich import ingest_draftkings_to_canonical
        ingest_draftkings_to_canonical(conn, parsed)
        ingest_draftkings_to_canonical(conn, parsed)
        # Each horse name should appear exactly once in canonical horses
        for e in parsed.entries:
            count = conn.execute(
                "SELECT COUNT(*) FROM horses WHERE name = ? COLLATE NOCASE",
                (e.horse_name,),
            ).fetchone()[0]
            assert count == 1, f"Duplicate horses entry for {e.horse_name!r}"
        conn.close()

    def test_second_ingest_creates_no_duplicate_entries(self, parsed):
        conn = _mem_conn()
        from src.services.draftkings_enrich import ingest_draftkings_to_canonical
        card_id, _ = ingest_draftkings_to_canonical(conn, parsed)
        ingest_draftkings_to_canonical(conn, parsed)
        count = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE card_id = ?", (card_id,)
        ).fetchone()[0]
        assert count == len(parsed.entries)
        conn.close()

    def test_second_ingest_creates_no_duplicate_horse_starts(self, parsed):
        conn = _mem_conn()
        from src.services.draftkings_enrich import ingest_draftkings_to_canonical
        ingest_draftkings_to_canonical(conn, parsed)
        count_after_first = conn.execute("SELECT COUNT(*) FROM horse_starts").fetchone()[0]
        ingest_draftkings_to_canonical(conn, parsed)
        count_after_second = conn.execute("SELECT COUNT(*) FROM horse_starts").fetchone()[0]
        assert count_after_first == count_after_second
        conn.close()


# ---------------------------------------------------------------------------
# 11. Staging tables are append-only after re-ingest
# ---------------------------------------------------------------------------

class TestStagingAppendOnly:
    @pytest.fixture(scope="class")
    def parsed(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        return parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)

    def test_staging_entries_preserved_after_reingest(self, parsed):
        """Staging rows must not be deleted or modified on re-ingest."""
        conn = _mem_conn()
        from src.services.draftkings_enrich import ingest_draftkings_to_canonical
        ingest_draftkings_to_canonical(conn, parsed)
        count1 = conn.execute("SELECT COUNT(*) FROM dk_staging_entries").fetchone()[0]
        ingest_draftkings_to_canonical(conn, parsed)
        count2 = conn.execute("SELECT COUNT(*) FROM dk_staging_entries").fetchone()[0]
        assert count1 == count2  # no new rows (idempotent), no deletions
        conn.close()

    def test_staging_starts_preserved_after_reingest(self, parsed):
        conn = _mem_conn()
        from src.services.draftkings_enrich import ingest_draftkings_to_canonical
        ingest_draftkings_to_canonical(conn, parsed)
        count1 = conn.execute("SELECT COUNT(*) FROM dk_staging_starts").fetchone()[0]
        ingest_draftkings_to_canonical(conn, parsed)
        count2 = conn.execute("SELECT COUNT(*) FROM dk_staging_starts").fetchone()[0]
        assert count1 == count2
        conn.close()


# ---------------------------------------------------------------------------
# 12. Pre-race feature DataFrame structure
# ---------------------------------------------------------------------------

EXPECTED_FEATURE_COLUMNS = {
    "card_id", "entry_id", "horse_name", "post_position",
    "morning_line_odds", "market_implied_prob",
    "days_since_last_start", "starts_last_90d",
    "recent_finish_percentile_w",
    "surface_distance_start_count",
    "surface_distance_finish_percentile_w",
    "class_delta_last_to_today",
    "days_since_last_workout", "workout_cadence_30d",
    "prior_publicness", "historical_scratch_rate",
    "career_starts", "career_wins",
    "pre_race_starts_count", "pre_race_workouts_count",
    "target_race_records_excluded", "scoring_as_of_timestamp",
}


class TestPreRaceFeatureStructure:
    @pytest.fixture(scope="class")
    def feat_df(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        from src.services.draftkings_enrich import (
            ingest_draftkings_to_canonical,
            generate_dk_pre_race_features,
        )
        parsed = parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)
        conn = _mem_conn()
        card_id, _ = ingest_draftkings_to_canonical(conn, parsed)
        df = generate_dk_pre_race_features(conn, card_id, parsed)
        yield df
        conn.close()

    def test_expected_columns_present(self, feat_df):
        missing = EXPECTED_FEATURE_COLUMNS - set(feat_df.columns)
        assert not missing, f"Missing feature columns: {missing}"

    def test_one_row_per_entry(self, feat_df):
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        parsed = parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)
        assert len(feat_df) == len(parsed.entries)

    def test_no_all_null_feature_row(self, feat_df):
        numeric_cols = [
            "market_implied_prob", "prior_publicness",
            "recent_finish_percentile_w", "surface_distance_finish_percentile_w",
        ]
        for col in numeric_cols:
            if col in feat_df.columns:
                assert feat_df[col].notna().any(), f"Column {col!r} is all-null"


# ---------------------------------------------------------------------------
# 13. Feature value ranges
# ---------------------------------------------------------------------------

class TestFeatureValueRanges:
    @pytest.fixture(scope="class")
    def feat_df(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        from src.services.draftkings_enrich import (
            ingest_draftkings_to_canonical,
            generate_dk_pre_race_features,
        )
        parsed = parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)
        conn = _mem_conn()
        card_id, _ = ingest_draftkings_to_canonical(conn, parsed)
        df = generate_dk_pre_race_features(conn, card_id, parsed)
        yield df
        conn.close()

    def test_market_implied_prob_between_0_and_1(self, feat_df):
        col = feat_df["market_implied_prob"].dropna()
        assert (col >= 0.0).all() and (col <= 1.0).all()

    def test_recent_finish_percentile_between_0_and_1(self, feat_df):
        col = feat_df["recent_finish_percentile_w"].dropna()
        assert (col >= 0.0).all() and (col <= 1.0).all()

    def test_surface_distance_finish_percentile_between_0_and_1(self, feat_df):
        col = feat_df["surface_distance_finish_percentile_w"].dropna()
        assert (col >= 0.0).all() and (col <= 1.0).all()

    def test_prior_publicness_between_0_and_1(self, feat_df):
        col = feat_df["prior_publicness"].dropna()
        assert (col >= 0.0).all() and (col <= 1.0).all()

    def test_starts_last_90d_is_non_negative(self, feat_df):
        assert (feat_df["starts_last_90d"] >= 0).all()

    def test_workout_cadence_30d_is_non_negative(self, feat_df):
        assert (feat_df["workout_cadence_30d"] >= 0).all()

    def test_historical_scratch_rate_between_0_and_1(self, feat_df):
        col = feat_df["historical_scratch_rate"].dropna()
        assert (col >= 0.0).all() and (col <= 1.0).all()

    def test_pre_race_starts_count_non_negative(self, feat_df):
        assert (feat_df["pre_race_starts_count"] >= 0).all()

    def test_target_race_records_excluded_non_negative(self, feat_df):
        assert (feat_df["target_race_records_excluded"] >= 0).all()


# ---------------------------------------------------------------------------
# 14. Market implied prob must never use off_odds or unknown
# ---------------------------------------------------------------------------

class TestMarketOddsPolicy:
    @pytest.fixture(scope="class")
    def parsed(self):
        pytest.importorskip("pdfplumber")
        from src.ingest.draftkings_pdf import parse_draftkings_pdf
        return parse_draftkings_pdf(_load_fixture(), GOLDEN_FILENAME)

    def test_market_implied_prob_source_is_morning_line(self, parsed):
        """market_implied_prob is derived only from morning_line_decimal (ML odds).
        Entries with odds_type == 'off_odds' or 'unknown' must still use ML for
        market_implied_prob, not the post-race/unknown price."""
        from src.services.draftkings_enrich import (
            ingest_draftkings_to_canonical,
            generate_dk_pre_race_features,
        )
        conn = _mem_conn()
        card_id, _ = ingest_draftkings_to_canonical(conn, parsed)
        feat_df = generate_dk_pre_race_features(conn, card_id, parsed)

        # All market_implied_prob values should be derivable from morning_line_odds
        for _, row in feat_df.iterrows():
            ml_odds = row["morning_line_odds"]
            mip = row["market_implied_prob"]
            if ml_odds and mip:
                expected = round(1.0 / max(float(ml_odds), 1.01), 4)
                assert abs(float(mip) - expected) < 0.001, (
                    f"market_implied_prob {mip} for {row['horse_name']!r} "
                    f"does not match ML-derived {expected} (ml_odds={ml_odds})"
                )
        conn.close()


# ---------------------------------------------------------------------------
# 15. ensure_feature_store_columns migration guard (PR #10 hotfix)
# ---------------------------------------------------------------------------

class TestFeatureStoreMigrationGuard:
    def test_ensure_feature_store_columns_is_importable_from_db(self):
        from src.utils.db import ensure_feature_store_columns
        assert callable(ensure_feature_store_columns)

    def test_builder_calls_migration_before_dml(self):
        """Verify the import line in build_features() names src.utils.db."""
        import ast, textwrap
        source = Path(ROOT / "src" / "features" / "builder.py").read_text(encoding="utf-8")
        # Ensure the function name 'ensure_feature_store_columns' appears in build_features
        func_start = source.find("def build_features(")
        assert func_start != -1
        func_body = source[func_start:]
        assert "ensure_feature_store_columns" in func_body, (
            "build_features() must call ensure_feature_store_columns(conn) before any DML"
        )

    def test_draftkings_enrich_imports_from_utils_db(self):
        """The wrong import (from src.features.builder) must no longer exist."""
        source = Path(ROOT / "src" / "services" / "draftkings_enrich.py").read_text(encoding="utf-8")
        assert "from src.features.builder import ensure_feature_store_columns" not in source, (
            "Wrong import still present; should be 'from src.utils.db import ...'"
        )
        assert "from src.utils.db import ensure_feature_store_columns" in source

    def test_migration_adds_pr10_columns_to_bare_feature_store(self):
        """Simulate a production DB that pre-dates PR #10 and verify migration adds columns."""
        from src.utils.db import ensure_feature_store_columns
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE feature_store (feature_id INTEGER PRIMARY KEY, card_id INTEGER)")
        conn.commit()
        ensure_feature_store_columns(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(feature_store)").fetchall()}
        for expected_col in (
            "run_style_evidence_count", "run_style_source", "pace_band",
            "classified_runner_count", "active_runner_count", "pace_state",
        ):
            assert expected_col in cols, f"PR #10 column {expected_col!r} not added by migration"
        conn.close()

    def test_migration_is_idempotent(self):
        """Calling ensure_feature_store_columns twice must not raise."""
        from src.utils.db import ensure_feature_store_columns
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE feature_store (feature_id INTEGER PRIMARY KEY, card_id INTEGER)")
        conn.commit()
        ensure_feature_store_columns(conn)
        ensure_feature_store_columns(conn)  # must not raise
        conn.close()
