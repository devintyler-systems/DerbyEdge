"""Derby Chaos Patch — codified from the 2026 Derby calibration.

Implements the spec from docs/derby_engine_darkhorse.md sections 1-4:

1) Apply favorite-archetype multipliers under high chaos.
2) Enforce DARK_HORSE_WIN_FLOOR for qualifying tail horses.
3) Move 5-10% of total win mass from over-trusted favorites to dark horses
   in high-ChaosIndex 3yo G1 routes.
4) Re-normalize and tag.

This module is RACE-FAMILY scoped: by default it activates on
Derby-family races (3yo G1 dirt routes, field >= 14, ChaosIndex >= 0.7),
but accepts an explicit override.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass


# --- Tunable parameters (can be re-tuned via backtesting) ---
CHAOS_MIN_REALLOCATION = 0.05
CHAOS_MAX_REALLOCATION = 0.10
DARK_HORSE_WIN_FLOOR = 0.035
CHAOS_INDEX_THRESHOLD = 0.70

# Favorite-archetype multipliers (chaos-on)
MULT_FAV_RAIL_CLOSER = 0.90       # Renegade-type
MULT_FAV_TACT_INNER = 0.95        # Commandment-type
MULT_FAV_TACT_OUTER = 1.05        # Further Ado-type

# DarkHorseFlag thresholds.
# Lower bound deliberately relaxed below the original 3% spec: Golden Tempo had
# WinProb_base ~2% and was the canonical chaos winner. We want to capture that
# tail without letting in pure no-hopers, so we cap at 1.5% on the bottom.
DARK_PROB_BAND_LO = 0.015
DARK_PROB_BAND_HI = 0.12
DARK_PUBLICNESS_MAX = 7.0


@dataclass
class PatchConfig:
    chaos_index: float
    enabled: bool = True
    min_realloc: float = CHAOS_MIN_REALLOCATION
    max_realloc: float = CHAOS_MAX_REALLOCATION
    dark_horse_floor: float = DARK_HORSE_WIN_FLOOR


def compute_dark_horse_flag(df: pd.DataFrame) -> pd.Series:
    """Boolean flag per row.

    Required cols: WinProb_base, DevCurve_score, FinishEnergy_score,
    PaceFit_score, DistanceProj_score, Publicness_score
    """
    cond = (
        (df['WinProb_base'].between(DARK_PROB_BAND_LO, DARK_PROB_BAND_HI)) &
        (df['Publicness_score'] <= DARK_PUBLICNESS_MAX) &
        ((df['DevCurve_score'] >= 7.0) | (df['FinishEnergy_score'] >= 7.0)) &
        ((df['PaceFit_score'] >= 6.0) | (df['DistanceProj_score'] >= 6.0))
    )
    return cond.fillna(False)


def compute_upside_score(df: pd.DataFrame) -> pd.Series:
    """0-1 scale from DevCurve and FinishEnergy."""
    raw = 0.5 * df['DevCurve_score'] + 0.5 * df['FinishEnergy_score']
    return (raw / 10.0).clip(0.0, 1.0)


def realloc_target(chaos_index: float) -> float:
    """How much total win mass to move from favorites -> dark horses."""
    if chaos_index < CHAOS_INDEX_THRESHOLD:
        return 0.0
    pct = CHAOS_MIN_REALLOCATION + (CHAOS_MAX_REALLOCATION - CHAOS_MIN_REALLOCATION) * \
          (chaos_index - CHAOS_INDEX_THRESHOLD) / (1.0 - CHAOS_INDEX_THRESHOLD)
    return float(np.clip(pct, CHAOS_MIN_REALLOCATION, CHAOS_MAX_REALLOCATION))


def apply_derby_chaos_patch(
    df_race: pd.DataFrame,
    chaos_index: float,
    config: PatchConfig | None = None,
) -> pd.DataFrame:
    """Apply the Derby chaos patch to one race's worth of rows.

    Required cols (all 0-10 unless noted):
      WinProb_base (0-1)
      DevCurve_score, FinishEnergy_score, PaceFit_score,
      DistanceProj_score, Publicness_score
      late_fig_z (z-score, can be negative)
      FavRailCloserFlag, FavTacticalInnerFlag, FavTacticalOuterFlag (bool/int)

    Returns df with added columns:
      DarkHorseFlag, UpsideScore_norm,
      WinProb_after_mult, WinProb_after_floor,
      WinProb_final, DarkHorseTier, chaos_beneficiary_flag
    """
    cfg = config or PatchConfig(chaos_index=chaos_index)
    df = df_race.copy().reset_index(drop=True)

    # Step 0: validate input probabilities
    base_total = df['WinProb_base'].sum()
    if base_total <= 0:
        raise ValueError("Sum of WinProb_base must be > 0")
    df['WinProb_base'] = df['WinProb_base'] / base_total  # ensure normalized

    # Step 1: dark horse flagging on PRE-patch numbers
    df['DarkHorseFlag'] = compute_dark_horse_flag(df)
    df['UpsideScore_norm'] = compute_upside_score(df)

    # Step 2: favorite-archetype multipliers (chaos-on)
    df['WinProb_after_mult'] = df['WinProb_base'].copy()
    if chaos_index >= CHAOS_INDEX_THRESHOLD and cfg.enabled:
        rail = df['FavRailCloserFlag'].astype(bool)
        ti = df['FavTacticalInnerFlag'].astype(bool)
        to = df['FavTacticalOuterFlag'].astype(bool)
        df.loc[rail, 'WinProb_after_mult'] *= MULT_FAV_RAIL_CLOSER
        df.loc[ti, 'WinProb_after_mult'] *= MULT_FAV_TACT_INNER
        df.loc[to, 'WinProb_after_mult'] *= MULT_FAV_TACT_OUTER

    # Step 3: enforce dark-horse floor for strong-tail horses
    df['WinProb_after_floor'] = df['WinProb_after_mult'].copy()
    if chaos_index >= CHAOS_INDEX_THRESHOLD and cfg.enabled:
        floor_eligible = (
            df['DarkHorseFlag'] &
            (df['UpsideScore_norm'] >= 0.7) &
            (df['late_fig_z'].fillna(-9) >= 0.7) &
            (df['PaceFit_score'] >= 7.0)
        )
        df.loc[floor_eligible, 'WinProb_after_floor'] = np.maximum(
            df.loc[floor_eligible, 'WinProb_after_floor'],
            cfg.dark_horse_floor,
        )

    # Step 4: chaos reallocation
    target = realloc_target(chaos_index) if cfg.enabled else 0.0
    df['chaos_beneficiary_flag'] = (
        df['DarkHorseFlag'] &
        (df['UpsideScore_norm'] >= 0.5) &
        (df['late_fig_z'].fillna(-9) >= 0.0) &
        (df['PaceFit_score'] >= 6.0)
    )

    df['WinProb_final'] = df['WinProb_after_floor'].copy()

    if target > 0 and df['chaos_beneficiary_flag'].any():
        # Donors: rail-closer + inner-tactical archetypes (chaos hurts them),
        # plus any remaining high-prob horse with low UpsideScore (overtrusted
        # chalk). The OUTER-tactical archetype is explicitly NOT a donor — it
        # already received a +5% chaos multiplier above and is the favored
        # profile in chaos.
        donor_arch_mask = (
            df['FavRailCloserFlag'].astype(bool)
            | df['FavTacticalInnerFlag'].astype(bool)
        )
        donor_mask = (
            ~df['DarkHorseFlag']
            & ~df['FavTacticalOuterFlag'].astype(bool)
            & (
                donor_arch_mask
                | (
                    (df['UpsideScore_norm'] <= 0.4)
                    & (df['WinProb_base'] >= df['WinProb_base'].quantile(0.7))
                )
            )
        )
        donors = df[donor_mask].sort_values('WinProb_base', ascending=False)
        # Recipients weighted by UpsideScore_norm * max(late_fig_z, 0.1)
        recipients = df[df['chaos_beneficiary_flag']].copy()
        recipients['weight'] = (
            recipients['UpsideScore_norm'] * recipients['late_fig_z'].clip(lower=0.1)
        )
        wsum = recipients['weight'].sum()
        if len(donors) and wsum > 0:
            # Pull pro-rata from donors
            donor_total = donors['WinProb_base'].sum()
            # Safety: never move more than the requested target, and never
            # take more than 60% of the donor pool's mass.
            move_total = min(target, donor_total * 0.6)
            if move_total > 0:
                pulls = donors['WinProb_base'] / donor_total * move_total
                df.loc[donors.index, 'WinProb_final'] -= pulls
                # Distribute to recipients
                gives = recipients['weight'] / wsum * move_total
                df.loc[recipients.index, 'WinProb_final'] += gives

    # Step 5: re-normalize
    final_total = df['WinProb_final'].sum()
    if final_total > 0:
        df['WinProb_final'] = df['WinProb_final'] / final_total

    # Step 6: tier label
    def _tier(row):
        if not row['DarkHorseFlag']:
            return 'none'
        return 'strong' if row['UpsideScore_norm'] >= 0.7 else 'light'
    df['DarkHorseTier'] = df.apply(_tier, axis=1)

    return df
