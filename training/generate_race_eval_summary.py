"""Generate a compact markdown summary from race_eval_by_segment.csv and race_eval_by_tier.csv."""

import csv
import os

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")
SEGMENT_CSV = os.path.join(OUTPUT_DIR, "race_eval_by_segment.csv")
TIER_CSV = os.path.join(OUTPUT_DIR, "race_eval_by_tier.csv")
SUMMARY_MD = os.path.join(OUTPUT_DIR, "race_eval_summary.md")


def _pct(val) -> str:
    if val is None or val == "":
        return "—"
    return f"{float(val) * 100:.1f}%"


def _fmt(val, decimals=2) -> str:
    if val is None or val == "":
        return "—"
    return f"{float(val):.{decimals}f}"


def _read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _tier_table(rows: list[dict]) -> list[str]:
    header = "| Tier | Races | TP Wins | TP Win Rate | Avg Eff TP Finish | Scr Orig TP Races | Scr Orig TP Win Rate |"
    sep    = "|------|-------|---------|-------------|-------------------|-------------------|----------------------|"
    lines = [header, sep]
    for r in rows:
        lines.append(
            f"| {r['tier_name']} | {r['races']} | {r['tp_wins']} "
            f"| {_pct(r['tp_win_rate'])} | {_fmt(r['avg_eff_tp_finish'])} "
            f"| {r['scratched_orig_tp_races']} | {_pct(r['scratched_orig_tp_win_rate'])} |"
        )
    return lines


def _segment_table(rows: list[dict]) -> list[str]:
    header = "| Tier | Surface | Dist | Field Bucket | Races | TP Wins | TP Win Rate | Avg Eff TP Finish |"
    sep    = "|------|---------|------|--------------|-------|---------|-------------|-------------------|"
    lines = [header, sep]
    for r in rows:
        lines.append(
            f"| {r['tier_name']} | {r['surface']} | {r['dist_category']} "
            f"| {r['field_size_bucket']} | {r['races']} | {r['tp_wins']} "
            f"| {_pct(r['tp_win_rate'])} | {_fmt(r['avg_eff_tp_finish'])} |"
        )
    return lines


def _flags(tier_rows: list[dict], seg_rows: list[dict]) -> list[str]:
    bullets = []

    tier_by_name = {r["tier_name"]: r for r in tier_rows}

    for r in tier_rows:
        if int(r["races"]) < 5:
            bullets.append(f"- Low sample: {r['tier_name']} has only {r['races']} races.")

    for r in seg_rows:
        n = int(r["races"])
        rate = float(r["tp_win_rate"]) if r["tp_win_rate"] not in (None, "") else 0.0
        if n >= 5 and rate >= 0.30:
            bullets.append(
                f"- Strong segment: {r['tier_name']} / {r['surface']} / {r['dist_category']} "
                f"/ {r['field_size_bucket']} won {_pct(r['tp_win_rate'])} over {n} races."
            )
        if n >= 5 and rate <= 0.12:
            bullets.append(
                f"- Weak segment: {r['tier_name']} / {r['surface']} / {r['dist_category']} "
                f"/ {r['field_size_bucket']} won {_pct(r['tp_win_rate'])} over {n} races."
            )

    for r in tier_rows:
        scr = int(r["scratched_orig_tp_races"]) if r["scratched_orig_tp_races"] not in (None, "") else 0
        if scr >= 3:
            scr_rate = float(r["scratched_orig_tp_win_rate"]) if r["scratched_orig_tp_win_rate"] not in (None, "") else 0.0
            tier_rate = float(r["tp_win_rate"]) if r["tp_win_rate"] not in (None, "") else 0.0
            if scr_rate >= tier_rate:
                bullets.append(f"- Scratch handling held up for {r['tier_name']}.")

    return bullets if bullets else ["- No notable flags at current sample size."]


def _policy_section() -> list[str]:
    return [
        "## Current policy defaults",
        "",
        "- Default tier: enriched_proxy",
        "- Tier override active: D / sprint / Small (<=6) -> enriched_proxy",
        "- Tier override active: D / sprint / Medium (7-9) -> enriched_proxy",
        "- Tier override active: D / sprint / Large (10-12) -> enriched_proxy",
        "- Default chaos: Off",
        "- Chaos override active: D / sprint / Small (<=6) -> Off",
        "- Turf remains on default behavior until larger sample arrives.",
        "",
    ]


def main():
    tier_rows = _read_csv(TIER_CSV)
    seg_rows  = _read_csv(SEGMENT_CSV)

    lines = ["# Race Eval Summary", ""]

    lines += ["## Tier performance", ""]
    lines += _tier_table(tier_rows)
    lines += [""]

    lines += ["## Segment performance", ""]
    lines += _segment_table(seg_rows)
    lines += [""]

    lines += ["## Flags", ""]
    lines += _flags(tier_rows, seg_rows)
    lines += [""]

    lines += _policy_section()

    os.makedirs(os.path.dirname(SUMMARY_MD), exist_ok=True)
    with open(SUMMARY_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Written: {SUMMARY_MD}")


if __name__ == "__main__":
    main()
