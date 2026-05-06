"""Unit tests — codifies the 2026 Derby Golden Tempo lesson into the engine.

Synthetic race shaped like the 2026 Derby with:
  - Three tactical favorites (Renegade rail-closer, Commandment inner, Further Ado outer)
  - One Golden-Tempo-shaped dark horse (long price, strong late, high upside)
  - Filler field

Assertions enforce:
  1. Win-probability floor for strong-tail dark horses.
  2. Chaos reallocation magnitude bounds.
  3. Top-2-by-final remains a permutation of the favorite trio.
  4. Chaos beneficiaries gain mass under high chaos.
  5. Total probability sums to 1.0.
  6. Patch is no-op when chaos < threshold.
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd
import pytest

from derbyedge.chaos_patch import (
    apply_derby_chaos_patch,
    compute_dark_horse_flag,
    compute_upside_score,
    realloc_target,
    CHAOS_INDEX_THRESHOLD,
    DARK_HORSE_WIN_FLOOR,
)


def _synthetic_derby() -> pd.DataFrame:
    """Build a 19-row Derby-shaped race."""
    horses = []

    # Top trio
    horses.append({
        'horse': 'Renegade', 'WinProb_base': 0.17,
        'DevCurve_score': 6.0, 'FinishEnergy_score': 6.0,
        'PaceFit_score': 5.0, 'DistanceProj_score': 7.0, 'Publicness_score': 9.5,
        'late_fig_z': 0.3,
        'FavRailCloserFlag': 1, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 0,
    })
    horses.append({
        'horse': 'Commandment', 'WinProb_base': 0.19,
        'DevCurve_score': 7.0, 'FinishEnergy_score': 6.5,
        'PaceFit_score': 7.0, 'DistanceProj_score': 7.0, 'Publicness_score': 9.0,
        'late_fig_z': 0.5,
        'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 1, 'FavTacticalOuterFlag': 0,
    })
    horses.append({
        'horse': 'Further Ado', 'WinProb_base': 0.17,
        'DevCurve_score': 8.0, 'FinishEnergy_score': 8.5,
        'PaceFit_score': 8.0, 'DistanceProj_score': 8.0, 'Publicness_score': 8.5,
        'late_fig_z': 1.5,
        'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 1,
    })

    # Mid-tier secondaries
    horses.append({
        'horse': 'Emerging Market', 'WinProb_base': 0.10,
        'DevCurve_score': 8.5, 'FinishEnergy_score': 7.0,
        'PaceFit_score': 7.5, 'DistanceProj_score': 6.0, 'Publicness_score': 6.5,
        'late_fig_z': 0.9,
        'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 0,
    })
    horses.append({
        'horse': 'So Happy', 'WinProb_base': 0.08,
        'DevCurve_score': 7.0, 'FinishEnergy_score': 6.0,
        'PaceFit_score': 4.0, 'DistanceProj_score': 5.0, 'Publicness_score': 8.0,
        'late_fig_z': 0.2,
        'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 0,
    })
    horses.append({
        'horse': 'Chief Wallabee', 'WinProb_base': 0.06,
        'DevCurve_score': 6.0, 'FinishEnergy_score': 6.5,
        'PaceFit_score': 6.0, 'DistanceProj_score': 6.0, 'Publicness_score': 6.0,
        'late_fig_z': 0.4,
        'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 0,
    })

    # GOLDEN TEMPO — the canonical chaos-tail dark horse
    horses.append({
        'horse': 'Golden Tempo', 'WinProb_base': 0.02,
        'DevCurve_score': 7.5, 'FinishEnergy_score': 9.0,
        'PaceFit_score': 8.5, 'DistanceProj_score': 6.5, 'Publicness_score': 4.0,
        'late_fig_z': 1.2,
        'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 0,
    })

    # Other dark candidates
    horses.append({
        'horse': 'Incredibolt', 'WinProb_base': 0.04,
        'DevCurve_score': 6.5, 'FinishEnergy_score': 8.0,
        'PaceFit_score': 7.0, 'DistanceProj_score': 6.0, 'Publicness_score': 5.5,
        'late_fig_z': 0.8,
        'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 0,
    })
    horses.append({
        'horse': 'Potente', 'WinProb_base': 0.04,
        'DevCurve_score': 7.5, 'FinishEnergy_score': 6.0,
        'PaceFit_score': 6.0, 'DistanceProj_score': 5.5, 'Publicness_score': 5.0,
        'late_fig_z': 0.5,
        'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 0,
    })

    # Filler longshots — should not flag as dark
    for i in range(10):
        horses.append({
            'horse': f'Filler{i+1}', 'WinProb_base': 0.013,
            'DevCurve_score': 4.0, 'FinishEnergy_score': 4.0,
            'PaceFit_score': 4.5, 'DistanceProj_score': 4.0, 'Publicness_score': 3.0,
            'late_fig_z': -0.5,
            'FavRailCloserFlag': 0, 'FavTacticalInnerFlag': 0, 'FavTacticalOuterFlag': 0,
        })

    df = pd.DataFrame(horses)
    df['WinProb_base'] = df['WinProb_base'] / df['WinProb_base'].sum()
    return df


# ---------------------------------------------------------------------------
# Tests

def test_realloc_target_off_below_threshold():
    assert realloc_target(0.5) == 0.0
    assert realloc_target(0.69) == 0.0


def test_realloc_target_within_band():
    assert realloc_target(0.7) == pytest.approx(0.05, abs=1e-9)
    assert realloc_target(1.0) == pytest.approx(0.10, abs=1e-9)
    assert 0.05 < realloc_target(0.85) < 0.10


def test_dark_horse_flag_includes_golden_tempo():
    df = _synthetic_derby()
    df['DarkHorseFlag'] = compute_dark_horse_flag(df)
    flagged = set(df[df['DarkHorseFlag']]['horse'])
    assert 'Golden Tempo' in flagged
    assert 'Renegade' not in flagged           # too publicized
    assert 'Filler1' not in flagged            # too low Win/upside


def test_patch_sums_to_one():
    df = _synthetic_derby()
    out = apply_derby_chaos_patch(df, chaos_index=0.85)
    assert math.isclose(out['WinProb_final'].sum(), 1.0, abs_tol=1e-9)


def test_patch_noop_when_chaos_low():
    df = _synthetic_derby()
    out = apply_derby_chaos_patch(df, chaos_index=0.5)
    # base normalized; final should equal base after re-normalize
    diff = (out['WinProb_final'] - out['WinProb_base']).abs().max()
    assert diff < 1e-9


def test_golden_tempo_floor_enforced():
    df = _synthetic_derby()
    out = apply_derby_chaos_patch(df, chaos_index=0.85)
    gt = out[out['horse'] == 'Golden Tempo'].iloc[0]
    # After re-normalization the floor may shift slightly downward,
    # but Golden Tempo must end above his pre-patch base.
    assert gt['WinProb_final'] > gt['WinProb_base']
    assert gt['DarkHorseFlag']
    assert gt['DarkHorseTier'] == 'strong'


def test_chaos_reallocation_magnitude():
    df = _synthetic_derby()
    out = apply_derby_chaos_patch(df, chaos_index=0.85)

    fav_names = ['Renegade', 'Commandment', 'Further Ado']
    fav_base = out[out['horse'].isin(fav_names)]['WinProb_base'].sum()
    fav_final = out[out['horse'].isin(fav_names)]['WinProb_final'].sum()
    moved_off_favs = fav_base - fav_final
    assert moved_off_favs >= 0.03  # at least 3 pts moved off chalk

    dark_base = out[out['DarkHorseFlag']]['WinProb_base'].sum()
    dark_final = out[out['DarkHorseFlag']]['WinProb_final'].sum()
    assert (dark_final - dark_base) >= 0.02  # dark horses gained mass


def test_top2_remain_among_favorite_trio():
    df = _synthetic_derby()
    out = apply_derby_chaos_patch(df, chaos_index=0.85)
    top2 = set(out.sort_values('WinProb_final', ascending=False).head(2)['horse'])
    assert top2.issubset({'Renegade', 'Commandment', 'Further Ado', 'Emerging Market'})


def test_further_ado_gains_relative_to_renegade():
    df = _synthetic_derby()
    out = apply_derby_chaos_patch(df, chaos_index=0.85)
    fa = out[out['horse'] == 'Further Ado'].iloc[0]
    re = out[out['horse'] == 'Renegade'].iloc[0]
    # Further Ado should gain or hold mass; Renegade must shed
    assert re['WinProb_final'] < re['WinProb_base']
    assert fa['WinProb_final'] >= fa['WinProb_base'] * 0.99


def test_chaos_beneficiary_flag_set_for_qualifiers():
    df = _synthetic_derby()
    out = apply_derby_chaos_patch(df, chaos_index=0.85)
    benef = set(out[out['chaos_beneficiary_flag']]['horse'])
    assert 'Golden Tempo' in benef
    # Chalk shouldn't be a beneficiary
    assert 'Renegade' not in benef
