# Source Contract Acquisition Rule

## Rule

A DK Markdown card is **score-candidate eligible** only when every active runner has:

| Field | Level |
|---|---|
| Horse identity | Runner |
| Post position | Runner |
| Morning line | Runner |
| Jockey | Runner |
| Trainer | Runner |
| Assigned weight | Runner |
| Target surface | Race |
| Target distance | Race |
| Target race date | Race |
| Target track identity | Race |

## If assigned weight is absent for one or more active runners

- Retain and audit the card.
- Label it `SOURCE_CONTRACT_INCOMPLETE`.
- Do not persist it to a live scoring path.
- Do not construct a score artifact.
- Do not produce probability, fair odds, market comparison, or wager tag.

## Acquisition instruction

Capture a source view that includes the program / weight column **before post time**, or attach a second same-race pre-post source artifact containing assigned weight and reconcile it by runner identity.

> **Do not derive weight from a later result chart or post-race artifact.**

## Minimum artifact set

For a card to advance to scoring readiness, two files are required:

```
{TRACK}_DK_Horse_R{n}_{M-D-YY}.md
{TRACK}_R{n}_{YYYY-MM-DD}_source_manifest.json
```

## Source manifest schema

```json
{
  "track_code": "TRACK",
  "race_number": 1,
  "race_date": "YYYY-MM-DD",
  "scheduled_post_timestamp": "YYYY-MM-DDTHH:MM:SS+00:00",
  "source_as_of_timestamp": "YYYY-MM-DDTHH:MM:SS+00:00",
  "source_provider": "draftkings_markdown",
  "source_tier": "OPERATOR_ATTESTED",
  "raw_file_name": "{TRACK}_DK_Horse_R{n}_{M-D-YY}.md",
  "raw_file_sha256": "<64-char hex SHA-256>",
  "field_status": "pre_race",
  "assigned_weight_status": "PRESENT_FOR_ALL_ACTIVE_RUNNERS"
}
```

### `assigned_weight_status` values

| Value | Meaning |
|---|---|
| `PRESENT_FOR_ALL_ACTIVE_RUNNERS` | Card satisfies the weight acquisition requirement |
| `ABSENT_FOR_ONE_OR_MORE_ACTIVE_RUNNERS` | Card is `SOURCE_CONTRACT_INCOMPLETE`; do not score |

## Current fixture status (Sep 7, 2026)

| Card | Weight status | Contract label | Score eligible |
|---|---|---|---|
| SAR Saratoga R6 (9-4-26) | PRESENT | `SOURCE_CONTRACT_COMPLETE` | Yes |
| DMR Del Mar R10 (9-7-26) | ABSENT (all 12 active runners) | `SOURCE_CONTRACT_INCOMPLETE` | No |

## Implementation

- `src/ingest/dk_source_contract.py` — rule, labels, manifest schema, builder, validator
- `tests/test_source_contract_acquisition_rule.py` — acceptance tests
- The existing `validate_draftkings_markdown_card()` independently rejects missing weight as an error; this module adds the formal label and manifest artifact layer on top.
