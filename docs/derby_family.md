# Derby Family Calibration Note

## Race-family scope

The Derby Chaos Patch applies to: **3-year-old G1 dirt routes, field size ≥ 14, ChaosIndex ≥ 0.7**.

Outside this family, the patch is a no-op (`apply_derby_chaos_patch(df, chaos_index=0.5)` returns base probabilities re-normalized).

## 2026 Derby — what we learned

In the 2026 Kentucky Derby, **Golden Tempo (post 19, ~30-1)** won off a fast pace
(22.68 / 46.44 / 1:10.90 in a 19-horse field), while the engine's A-tier (Commandment,
Further Ado, Renegade) ran well but missed:

| Horse | Pre-patch base | Final Position |
|---|---|---|
| Commandment | ~19% | 7th |
| Renegade | ~17% | **2nd** |
| Further Ado | ~17% | 11th |
| Emerging Market | ~10% | 10th |
| **Golden Tempo** | **~2%** | **1st** |

The base model under-allocated win mass to high-Upside, high-late-fig closers in
high-ChaosIndex races. The DerbyChaosPatch institutionalizes the response:

1. Increases reallocation bandwidth (5–10% of race win-mass moves to dark horses).
2. Enforces a 3.5% floor for Golden-Tempo-shaped tails.
3. Applies archetype-specific multipliers: rail-closers and inner tacticals get shaved;
   outer tacticals (Further Ado profile) get a small boost.

This ensures future Derbies automatically price in the same tail risk that decided the 2026 result.

## What the engine still missed (carry-forward backlog)

- **Renegade-as-runner-up was real** — the patch shaves him, but the model still ranked him too high pre-shave. Improvement: better trip-variance modeling for rail closers in 18+ fields.
- **Ocelli (3rd, 70-1, late add)** — wasn't flagged because we don't model AE-ins separately. Add a `late_add_flag` and corresponding upside boost.
- **Beyer hierarchy was a red herring** — Further Ado's 106 Blue Grass figure was field-quality-inflated. Feature engineering needs a `figure_quality_adjust` based on prep-race purse / grade / field strength.

## Calibration target (for the next Derby-family race)

When re-running 2026 Derby through the patched pipeline with proper chaos_index,
the expected post-patch slice should look approximately like:

| Horse | Post-patch target |
|---|---|
| Commandment | 16-17% |
| Renegade | 15-16% |
| Further Ado | 18-19% |
| Emerging Market | 10-11% |
| So Happy | ~8% |
| Golden Tempo | **5-6%** |
| Incredibolt / Potente | each +0.5-1.0pt |

Total moved to chaos horses: ≈ 6-8%. Tune `CHAOS_MAX_REALLOCATION` and
`DARK_HORSE_WIN_FLOOR` to hit this when actual feature inputs are available.
