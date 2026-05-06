"""Equibase XML parser.

Reads SIMD entry race-card XML files and yields normalized records ready for
SQLite insertion. Pre-race-only — no post-race result feature is leaked into
today's entry record.

Equibase encoding notes:
- Times: stored as integer hundredths of a second (e.g., 4894 = 48.94s).
- Lengths: integer hundredths (e.g., 275 = 2.75 lengths).
- Odds: integer; divide by 100 for dollars (1000 = 10.00 = 9-1).
- Distances: DistanceId in yards when DistanceUnit=Y, furlongs*100 when F, etc.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator
import re


def _t(elem, path: str, default: str | None = None) -> str | None:
    """Return text at xpath, stripped, or default."""
    if elem is None:
        return default
    n = elem.find(path)
    if n is None or n.text is None:
        return default
    return n.text.strip() or default


def _i(elem, path: str) -> int | None:
    v = _t(elem, path)
    if v is None or v == '':
        return None
    try:
        return int(v)
    except ValueError:
        try:
            return int(float(v))
        except ValueError:
            return None


def _f(elem, path: str) -> float | None:
    v = _t(elem, path)
    if v is None or v == '':
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _date(elem, path: str) -> str | None:
    v = _t(elem, path)
    if not v:
        return None
    # Equibase emits "2023-09-22+00:00" — keep ISO date only
    m = re.match(r'(\d{4}-\d{2}-\d{2})', v)
    return m.group(1) if m else v


def _val(elem, path: str) -> str | None:
    """Pull the .Value subnode at path (Equibase code-pair pattern)."""
    return _t(elem, f'{path}/Value')


def _desc(elem, path: str) -> str | None:
    return _t(elem, f'{path}/Description')


def parse_file(xml_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Parse a single SIMD XML file. Returns dict of table_name -> list[row]."""
    xml_path = Path(xml_path)
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Filename hint: SIMD20230101AQU_USA.xml -> track_id=AQU, date=2023-01-01
    m = re.match(r'SIMD(\d{4})(\d{2})(\d{2})([A-Z0-9]+)_(\w+)\.xml', xml_path.name)
    if not m:
        raise ValueError(f"Unrecognized SIMD filename: {xml_path.name}")
    file_year, file_month, file_day, file_track, file_country = m.groups()
    file_date = f"{file_year}-{file_month}-{file_day}"

    out = {
        'tracks': {},        # track_id -> row
        'people': {},        # external_party_id -> row
        'horses': {},        # registration_number -> row
        'races': [],
        'entries': [],
        'horse_starts': [],
        'fractions': [],
        'point_of_call': [],
        'company_line': [],
        'workouts': [],
    }

    # tracks reference table — use file hint
    out['tracks'][file_track] = {
        'track_id': file_track,
        'track_name': None,
        'country': file_country,
    }

    for race_elem in root.findall('Race'):
        race_no = _i(race_elem, 'RaceNumber')
        if race_no is None:
            continue
        race_id = f"{file_track}|{file_date}|{race_no}"

        race_row = {
            'race_id': race_id,
            'track_id': file_track,
            'race_date': file_date,
            'race_number': race_no,
            'day_evening': _t(race_elem, 'DayEvening'),
            'breed_type': _val(race_elem, 'BreedType'),
            'course_type': _val(race_elem, 'Course/CourseType'),
            'surface': _val(race_elem, 'Course/Surface'),
            'distance_id': _i(race_elem, 'Distance/DistanceId'),
            'distance_unit': _val(race_elem, 'Distance/DistanceUnit'),
            'distance_published': _t(race_elem, 'Distance/PublishedValue'),
            'about_distance': _t(race_elem, 'Distance/AboutDistanceIndicator'),
            'age_restriction': _t(race_elem, 'AgeRestriction/Value') or _t(race_elem, 'AgeRestriction'),
            'sex_restriction': _val(race_elem, 'SexRestriction'),
            'race_type': _t(race_elem, 'RaceType/RaceType'),
            'race_type_desc': _t(race_elem, 'RaceType/Description'),
            'race_name': _t(race_elem, 'RaceName'),
            'grade': _t(race_elem, 'Grade'),
            'purse_usa': _f(race_elem, 'PurseUSA'),
            'min_claim_price': _f(race_elem, 'MinimumClaimPrice'),
            'max_claim_price': _f(race_elem, 'MaximumClaimPrice'),
            'post_time': _t(race_elem, 'PostTime'),
            'number_of_runners': _i(race_elem, 'NumberOfRunners'),
            'conditions_text': _t(race_elem, 'ConditionText') or _t(race_elem, 'ConditionsOfRace'),
        }
        out['races'].append(race_row)

        for starter in race_elem.findall('Starters'):
            _parse_starter(starter, race_id, file_track, out)

    return out


def _parse_starter(starter, race_id: str, track_id: str, out: dict) -> None:
    horse = starter.find('Horse')
    if horse is None:
        return
    horse_reg = _t(horse, 'RegistrationNumber')
    if not horse_reg:
        return

    # horse master record
    if horse_reg not in out['horses']:
        sire = horse.find('Sire')
        dam = horse.find('Dam')
        dam_sire = dam.find('Sire') if dam is not None else None
        out['horses'][horse_reg] = {
            'registration_number': horse_reg,
            'horse_name': _t(horse, 'HorseName'),
            'foaling_date': _date(horse, 'FoalingDate'),
            'year_of_birth': _i(horse, 'YearOfBirth'),
            'foaling_area': _t(horse, 'FoalingArea'),
            'breed_type': _val(horse, 'BreedType'),
            'color': _val(horse, 'Color'),
            'sex': _val(horse, 'Sex'),
            'breeder_name': _t(horse, 'BreederName'),
            'sire_reg': _t(sire, 'RegistrationNumber') if sire is not None else None,
            'sire_name': _t(sire, 'HorseName') if sire is not None else None,
            'dam_reg': _t(dam, 'RegistrationNumber') if dam is not None else None,
            'dam_name': _t(dam, 'HorseName') if dam is not None else None,
            'dam_sire_reg': _t(dam_sire, 'RegistrationNumber') if dam_sire is not None else None,
            'dam_sire_name': _t(dam_sire, 'HorseName') if dam_sire is not None else None,
        }

    program_number = _t(starter, 'ProgramNumber')
    entry_id = f"{race_id}|{program_number}"
    out['entries'].append({
        'entry_id': entry_id,
        'race_id': race_id,
        'program_number': program_number,
        'post_position': _i(starter, 'PostPosition'),
        'horse_reg': horse_reg,
        'weight_carried': _i(starter, 'WeightCarried'),
        'coupled_indicator': _t(starter, 'CoupledIndicator'),
        'couple_type': _t(starter, 'CoupleType'),
        'equipment_code': _val(starter, 'Equipment'),
        'apprentice_type': _val(starter, 'ApprenticeType'),
        'apprentice_wt_allow': _i(starter, 'ApprenticeWeightAllowance'),
        'eligibility_text': _t(starter, 'EligibilityText'),
    })

    # Workouts for this starter
    for i, wk in enumerate(starter.findall('Workout')):
        wk_date = _date(wk, 'WorkoutDate') or _date(wk, 'Date')
        wk_track = _t(wk, 'Track/TrackID') or _t(wk, 'TrackID')
        out['workouts'].append({
            'workout_id': f"{entry_id}|{wk_date}|{i}",
            'entry_id': entry_id,
            'horse_reg': horse_reg,
            'workout_date': wk_date,
            'workout_track_id': wk_track,
            'distance_id': _i(wk, 'Distance/DistanceId'),
            'distance_unit': _val(wk, 'Distance/DistanceUnit'),
            'course_type': _val(wk, 'Course/CourseType'),
            'surface': _val(wk, 'Course/Surface'),
            'track_condition': _val(wk, 'TrackCondition'),
            'workout_time': _i(wk, 'WorkoutTime') or _i(wk, 'Time'),
            'type_of_workout': _t(wk, 'TypeOfWorkout/Value') or _t(wk, 'TypeOfWorkout'),
            'rank_in_set': _i(wk, 'RankInSet') or _i(wk, 'Rank'),
            'set_size': _i(wk, 'SetSize'),
            'workout_note': _t(wk, 'WorkoutNote') or _t(wk, 'Note'),
        })

    # Past performances
    for pp in starter.findall('PastPerformance'):
        _parse_past_performance(pp, horse_reg, entry_id, out)


def _parse_past_performance(pp, horse_reg: str, entry_id: str, out: dict) -> None:
    pp_track = _t(pp, 'Track/TrackID')
    pp_country = _t(pp, 'Track/Country')
    pp_date = _date(pp, 'RaceDate')
    pp_race_no = _i(pp, 'RaceNumber')
    if not (pp_date and pp_race_no is not None):
        return
    start_id = f"{horse_reg}|{pp_date}|{pp_track}|{pp_race_no}"

    start = pp.find('Start')
    jockey = start.find('Jockey') if start is not None else None
    trainer = start.find('Trainer') if start is not None else None
    owner = start.find('Owner') if start is not None else None

    # register people
    for p, src in [(jockey, 'JE'), (trainer, 'TE'), (owner, 'O6')]:
        if p is None:
            continue
        pid = _i(p, 'ExternalPartyId')
        if pid is None or pid in out['people']:
            continue
        out['people'][pid] = {
            'external_party_id': pid,
            'type_source': _t(p, 'TypeSource') or src,
            'first_name': _t(p, 'FirstName'),
            'middle_name': _t(p, 'MiddleName'),
            'last_name': _t(p, 'LastName'),
        }

    # register pp track
    if pp_track and pp_track not in out['tracks']:
        out['tracks'][pp_track] = {
            'track_id': pp_track,
            'track_name': _t(pp, 'Track/TrackName'),
            'country': pp_country,
        }

    out['horse_starts'].append({
        'start_id': start_id,
        'horse_reg': horse_reg,
        'entry_id': entry_id,
        'pp_track_id': pp_track,
        'pp_country': pp_country,
        'pp_race_date': pp_date,
        'pp_race_number': pp_race_no,
        'pp_breed_type': _t(pp, 'BreedType') or _val(pp, 'BreedType'),
        'jump_flag': _t(pp, 'JumpFlag'),
        'official_indicator': _t(pp, 'OfficialIndicator'),
        'pp_race_type': _t(pp, 'RaceType/RaceType'),
        'pp_course_type': _val(pp, 'Course/CourseType'),
        'pp_surface': _val(pp, 'Course/Surface'),
        'pp_distance_id': _i(pp, 'Distance/DistanceId'),
        'pp_distance_unit': _val(pp, 'Distance/DistanceUnit'),
        'pp_distance_published': _t(pp, 'Distance/PublishedValue'),
        'pp_grade': _t(pp, 'Grade'),
        'pp_stakes_indicator': _t(pp, 'StakesIndicator'),
        'pp_age_restriction': _t(pp, 'AgeRestriction'),
        'pp_max_claim_price': _f(pp, 'MaximumClaimingPrice'),
        'pp_purse_usa': _f(pp, 'PurseUSA'),
        'pp_track_condition': _val(pp, 'TrackCondition'),
        'pp_off_turf': _t(pp, 'OffTurfIndicator'),
        'pp_weather': _t(pp, 'Weather'),
        'pp_temperature': _i(pp, 'Temperature'),
        'pp_wind_speed': _i(pp, 'WindSpeed'),
        'pp_wind_direction': _t(pp, 'WindDirection'),
        'pp_n_starters': _i(pp, 'NumberOfStarters'),
        'pp_temp_rail_distance': _i(pp, 'TemporaryRailDistance'),
        'pp_run_up_distance': _i(pp, 'RunUpDistance'),
        'pp_timer_type': _t(pp, 'TimerType'),
        'pp_race_name': _t(pp, 'RaceName'),
        'pp_race_comment': _t(pp, 'RaceComment'),
        'pp_division': _t(pp, 'Division'),
        # Start (horse's own line)
        'weight_carried': _i(start, 'WeightCarried') if start is not None else None,
        'medication_code': _val(start, 'Medication') if start is not None else None,
        'equipment_code': _val(start, 'Equipment') if start is not None else None,
        'earnings_usa': _f(start, 'EarningsUSA') if start is not None else None,
        'jockey_id': _i(jockey, 'ExternalPartyId') if jockey is not None else None,
        'trainer_id': _i(trainer, 'ExternalPartyId') if trainer is not None else None,
        'owner_id': _i(owner, 'ExternalPartyId') if owner is not None else None,
        'odds_int': _i(start, 'Odds') if start is not None else None,
        'favorite': _t(start, 'Favorite') if start is not None else None,
        'nonbetting': _t(start, 'NonbettingIndicator') if start is not None else None,
        'coupled_indicator': _t(start, 'CoupledIndicator') if start is not None else None,
        'coupled_finish': _i(start, 'CoupledFinish') if start is not None else None,
        'post_position': _i(start, 'PostPosition') if start is not None else None,
        'program_number': _t(start, 'ProgramNumber') if start is not None else None,
        'official_finish': _i(start, 'OfficialFinish') if start is not None else None,
        'race_rating': _i(start, 'RaceRating') if start is not None else None,
        'class_rating': _i(start, 'ClassRating') if start is not None else None,
        'pace_figure_1': _i(start, 'PaceFigure1') if start is not None else None,
        'pace_figure_2': _i(start, 'PaceFigure2') if start is not None else None,
        'pace_figure_3': _i(start, 'PaceFigure3') if start is not None else None,
        'speed_figure': _i(start, 'SpeedFigure') if start is not None else None,
        'dead_heat_flag': _t(start, 'DeadHeatFlag') if start is not None else None,
        'claim_price_usa': _f(start, 'ClaimPriceUSA') if start is not None else None,
        'claimed_flag': _t(start, 'ClaimedFlag') if start is not None else None,
        'time_of_horse': _i(start, 'TimeOfHorse') if start is not None else None,
        'dq_indicator': _t(start, 'DQIndicator') if start is not None else None,
        'placed_indicator': _t(start, 'PlacedIndicator') if start is not None else None,
        'short_comment': _t(start, 'ShortComment') if start is not None else None,
        'long_comment': _t(start, 'LongComment') if start is not None else None,
    })

    # Fractions
    for fr in pp.findall('Fractions'):
        out['fractions'].append({
            'start_id': start_id,
            'fraction_label': _t(fr, 'Fraction'),
            'time_int': _i(fr, 'Time'),
            'fraction_print': _t(fr, 'FractionPrint'),
        })

    # Point of call (under Start)
    if start is not None:
        for poc in start.findall('PointOfCall'):
            out['point_of_call'].append({
                'start_id': start_id,
                'point_of_call': _t(poc, 'PointOfCall'),
                'position_int': _i(poc, 'Position'),
                'lengths_ahead': _i(poc, 'LengthsAhead'),
                'lengths_behind': _i(poc, 'LengthsBehind'),
                'print_flag': _t(poc, 'PointOfCallPrint'),
            })

    # Company line (top-3 finishers)
    for cl in pp.findall('CompanyLine'):
        op = _i(cl, 'OfficialPosition')
        if op is None:
            continue
        out['company_line'].append({
            'start_id': start_id,
            'horse_name': _t(cl, 'HorseName'),
            'weight_carried': _i(cl, 'WeightCarried'),
            'lengths_ahead': _i(cl, 'LengthsAheadAtFinish'),
            'position_at_finish': _i(cl, 'PositionAtFinish'),
            'official_position': op,
        })


def parse_directory(dirpath: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Parse all SIMD*.xml files under dirpath, merging deduped reference tables."""
    dirpath = Path(dirpath)
    merged = {
        'tracks': {}, 'people': {}, 'horses': {},
        'races': [], 'entries': [], 'horse_starts': [],
        'fractions': [], 'point_of_call': [], 'company_line': [], 'workouts': [],
    }
    for f in sorted(dirpath.glob('SIMD*.xml')):
        d = parse_file(f)
        for k, v in d.items():
            if isinstance(v, dict):
                merged[k].update(v)
            else:
                merged[k].extend(v)
    return merged
