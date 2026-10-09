# Track registry

One canonical code per facility, resolved from any spelling the sources use.

| File (`data/reference/`) | What it holds |
|---|---|
| `equibase_track_abbreviations.csv` | 342 codes from the Equibase "North American Racetrack Abbreviations" list, with a `kind`: `RACETRACK`, `FAIR`, `TRAINING` (TC), `FARM`. `kind` is assigned from the name (TC / Farm / County Fair) and corrected by hand where wrong. |
| `track_additions.csv` | Facilities missing from the Equibase list, with operator-supplied codes (Belterra Park BTP, Mahoning Valley MVR, WinStar Training Center WSR, Lynwood Stable LYN, Bolo Farm BLF). |
| `track_aliases.csv` | Extra spellings (mostly DraftKings long names, e.g. "Belmont at the Big A" -> BEL, "Hollywood Casino at Charles Town Races" -> CT). |
| `track_timezones.csv` | IANA timezone per code; used to turn a track-local post time into UTC. A track without a row cannot produce a pre-post timestamp. |

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

None in the repo's DK fixtures. Any new unregistered name fails validation with its text; add it to `track_additions.csv`.
The five operator-supplied codes were not independently checked against Equibase.

The source list is older than the 2026 season (it still has Hollywood Park, Arlington Park, Calder) and has no state
or timezone column; the timezone file covers the curated tracks plus the active ones whose zone is not in doubt.
