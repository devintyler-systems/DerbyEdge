from __future__ import annotations

import json

from src.ingest.run_state import RunMode
from src.services.feature_state import model_config_for_card
from src.services.run_mode import get_card_run_state


def test_immutable_limited_audit_promotes_to_effective_model_ready(mem_conn, tmp_path):
    track_id = mem_conn.execute(
        "INSERT INTO tracks (name, abbrev) VALUES ('Saratoga', 'SAR')"
    ).lastrowid
    card_id = mem_conn.execute(
        """INSERT INTO race_cards
               (track_id, card_date, race_number, distance_yards, surface, field_size)
           VALUES (?, '2026-09-02', 8, 1430, 'dirt', 2)""",
        (track_id,),
    ).lastrowid

    entry_ids = []
    horse_ids = []
    for post, name in enumerate(("Alpha", "Bravo"), start=1):
        horse_id = mem_conn.execute(
            "INSERT INTO horses (name) VALUES (?)", (name,)
        ).lastrowid
        entry_id = mem_conn.execute(
            """INSERT INTO entries
                   (card_id, horse_id, post_position, morning_line_odds)
               VALUES (?, ?, ?, ?)""",
            (card_id, horse_id, post, float(post + 2)),
        ).lastrowid
        horse_ids.append(horse_id)
        entry_ids.append(entry_id)

    config = model_config_for_card(mem_conn, card_id)
    required = list(dict.fromkeys(
        name
        for group in config["feature_groups"].values()
        for name in group["features"]
    ))
    columns = [
        "card_id", "entry_id", "horse_id", "horse_name", "post_position", "build_ts",
        *required,
    ]
    placeholders = ",".join("?" for _ in columns)
    for index, (entry_id, horse_id, name) in enumerate(
        zip(entry_ids, horse_ids, ("Alpha", "Bravo")), start=1
    ):
        feature_values = [0.1 * index + offset * 0.01 for offset in range(len(required))]
        mem_conn.execute(
            f"INSERT INTO feature_store ({','.join(columns)}) VALUES ({placeholders})",
            [card_id, entry_id, horse_id, name, index, "2026-09-02T20:30:00Z", *feature_values],
        )

    mem_conn.execute(
        """CREATE TABLE live_odds (
               captured_at TEXT, book_id TEXT, card_id INTEGER, entry_id INTEGER,
               post_position INTEGER, decimal_odds REAL, is_scratched INTEGER DEFAULT 0,
               is_morning_line INTEGER DEFAULT 0
           )"""
    )
    mem_conn.executemany(
        "INSERT INTO live_odds VALUES (?, 'manual', ?, ?, ?, ?, 0, 0)",
        [
            ("2026-09-02T20:31:00Z", card_id, entry_ids[0], 1, 3.2),
            ("2026-09-02T20:31:00Z", card_id, entry_ids[1], 2, 4.4),
        ],
    )
    mem_conn.commit()

    run_dir = tmp_path / "upload-001"
    run_dir.mkdir()
    audit = {
        "run_id": "upload-001",
        "run_mode": RunMode.MODEL_READY_LIMITED.value,
        "ingest_run_mode": RunMode.MODEL_READY_LIMITED.value,
        "source_provider": "1stbet",
        "field_size_declared": 2,
        "entries_parsed": 2,
        "entries_with_pp_history": 2,
        "starter_match_rate": 1.0,
        "race_metadata_complete": True,
        "has_morning_lines": True,
        "blocking_errors": [],
        "warnings": ["Immutable ingest-time warning."],
    }
    audit_path = run_dir / "feature_audit.json"
    audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    (run_dir / "card_binding.json").write_text(
        json.dumps({"run_id": "upload-001", "card_id": card_id}) + "\n",
        encoding="utf-8",
    )
    before = audit_path.read_bytes()

    state = get_card_run_state(mem_conn, card_id, runs_root=tmp_path)

    assert state.mode == RunMode.MODEL_READY
    assert state.audit["run_mode"] == RunMode.MODEL_READY_LIMITED.value
    assert state.quality.has_live_odds is True
    assert state.quality.required_model_features_complete is True
    assert state.feature_verification.passed is True
    assert audit_path.read_bytes() == before

    # Promotion is based on the current complete snapshot, not accumulated
    # historical quotes. A newer partial snapshot demotes the effective mode.
    mem_conn.execute(
        "INSERT INTO live_odds VALUES (?, 'manual', ?, ?, ?, ?, 0, 0)",
        ("2026-09-02T20:32:00Z", card_id, entry_ids[0], 1, 3.0),
    )
    mem_conn.commit()
    partial_state = get_card_run_state(mem_conn, card_id, runs_root=tmp_path)
    assert partial_state.mode == RunMode.MODEL_READY_LIMITED
    assert partial_state.quality.has_live_odds is False
