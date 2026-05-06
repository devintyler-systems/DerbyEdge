# Race-day odds CSV — template guide

Use `samples/race_day_template.csv` as your starting point.

## Required columns

| Column            | Required | Notes |
|-------------------|----------|-------|
| `book_id`         | yes      | `fanduel`, `draftkings`, `twinspires`, `morningline`, etc. Lowercased on ingest. |
| `race_id`         | yes      | `TRACK|YYYY-MM-DD|RACE_NUM` — e.g. `CD|2026-05-02|12`. Must match a race in DB. |
| `program_number`  | yes      | String. Coupled entries use `1A`, `2B`, etc. |
| `decimal_odds`    | one of   | e.g. `5.5`. Preferred — most precise. |
| `american_odds`   | one of   | e.g. `+450` or `-110`. |
| `morning_line`    | one of   | e.g. `5-2`. Set `is_morning_line=1`. |
| `fractional`      | one of   | e.g. `9/2`. |
| `is_morning_line` | optional | `1` = official ML row. |
| `is_scratched`    | optional | `1` = scratched. |
| `captured_at`     | optional | ISO-8601 UTC. Blank = now. Used for drift sparklines. |

Provide exactly **one** odds column per row. Decimal preferred.

## FanDuel CSV mapping

If you grab a FanDuel race card export:

| FanDuel column      | → maps to       |
|---------------------|-----------------|
| `Horse #`           | `program_number` |
| `Decimal Odds`      | `decimal_odds`   |
| (constant)          | `book_id` = `fanduel` |
| (constant per race) | `race_id` = `TRACK|YYYY-MM-DD|RACE_NUM` |
| (set to capture)    | `captured_at`    |

## Multi-book is fine

Stack rows from FanDuel, DraftKings, TwinSpires in the same file. The engine
de-vigs across books and surfaces the best price + drift per horse.

## Multi-snapshot is fine

Re-upload the same race throughout the day with later `captured_at` values
to populate the drift chart.
