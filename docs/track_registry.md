# Track registry

One canonical code per facility, resolved from any spelling the sources use.

| File (`data/reference/`) | What it holds |
|---|---|
| `equibase_track_abbreviations.csv` | 342 codes from the Equibase "North American Racetrack Abbreviations" list, with a `kind`: `RACETRACK`, `FAIR`, `TRAINING` (TC), `FARM`. `kind` is assigned from the name (TC / Farm / County Fair) and corrected by hand where wrong. |
| `source_listing_tracks.csv` | 337 rows from the "Listing of Thoroughbred/Quarter Horse Tracks": code, state/country, timezone offset from Pacific, name. Includes a separate `BAQ` code for "Belmont At The Big A". |
| `source_equineline_tracks.csv` | 370 rows from the Jockey Club Equineline "North American Track Codes" directory: code, name, city/state, results/charts flag, renaming notes (e.g. `AP` was Arlington Park before 1989). Covers historic tracks that appear in horses' past performances. |
| `track_timezones_derived.csv` | Zones derived from the listing, only where state and offset agree (see below). |
| `track_additions.csv` | Facilities missing from the Equibase list, with operator-supplied codes (Belterra Park BTP, Mahoning Valley MVR, WinStar Training Center WSR, Lynwood Stable LYN, Bolo Farm BLF). |
| `track_aliases.csv` | Extra spellings (mostly DraftKings long names, e.g. "Belmont at the Big A" -> BEL, "Hollywood Casino at Charles Town Races" -> CT). |
| `track_timezones.csv` | IANA timezone per code; used to turn a track-local post time into UTC. A track without a row cannot produce a pre-post timestamp. |

Source priority (a lower source can add codes and spellings but can never change what an existing name resolves to):
curated tracks > Equibase list > operator additions / aliases > track listing and Equineline. Two codes that
share a name only in the lower sources (for example `AD` and `AZD`, both "Arizona Downs") stay `ambiguous`.
All three lists agree on codes; the only name differences on shared codes are the same facility under another
name (fairs listed by town, "Ocala TC" / "Ocala Training Center").

`kind` also has `OTHER` for wager and special pseudo-tracks (`CCP` Cross Country Pick Four, `EQA` Equibase Special,
`BCA` Breeders' Cup Special); these are never race venues.

Timezones: `track_timezones.csv` (hand-maintained) wins over `track_timezones_derived.csv`. The listing's offset
alone is not trusted (it uses 0 as a placeholder: Arapahoe Park CO and Les Bois ID both show 0), so a zone is derived
only when the state and the offset agree, or for a split state when a non-zero offset picks the zone. 86 rows
disagree and are left blank, which makes the engine report "no timezone" instead of computing a wrong post time.

Regenerate the extracts with `python scripts/extract_track_sources.py --listing <pdf> --equineline <pdf>`.
The Equineline directory is © The Jockey Club Information Systems; it is used here as reference data only.

Resolution order (`src/derbyedge/tracks.py::resolve_track`): explicit code, exact name, Equibase-spelling
variants (`TC` = Training Center, `Th'ghbred` = Thoroughbred, `Farms`/`Farm`, ...), a bare code written as a name
(`BEL`), generic words dropped (`Race Course`, `Racetrack`, `Training Center`, `Farm`), then fuzzy match against
racetracks only. A name that matches two codes at the same level is `ambiguous` and is never guessed.

Behaviour that matters for ingestion:

* A DK race header must resolve to a `RACETRACK` or `FAIR`. Otherwise validation fails and names the track;
  there is no invented fallback code. Workout / PP track names that do not resolve are stored as written.
* `TRACK_CODES` / `TRACK_CODES_UPPER` (used by the PDF header scan) still cover only the curated tracks. Short names
  in the full list (Ely, Peg, ...) would cause false matches in free text.

## Adding a track

1. Find its Equibase code. Add a row to `track_additions.csv` (`code,name,kind,source`); leave `equibase_track_abbreviations.csv` as the source list.
2. Add any other spelling to `track_aliases.csv`.
3. Add its zone to `track_timezones.csv`.
4. Run `python -m pytest tests/test_track_registry.py`.

## Known gaps

None in the repo's DK fixtures. `Belmont at the Big A` is `BAQ` and `Belmont Park` is `BEL`: two different tracks, kept apart so track-specific history and race keys are not merged. If a source tags the Big A races `BEL`, change the two `BAQ` rows in `track_aliases.csv`. Any new unregistered name fails validation with its text; add it to `track_additions.csv`.
The five operator-supplied codes were not independently checked against Equibase.

The source list is older than the 2026 season (it still has Hollywood Park, Arlington Park, Calder) and has no state
or timezone column; the timezone file covers the curated tracks plus the active ones whose zone is not in doubt.
