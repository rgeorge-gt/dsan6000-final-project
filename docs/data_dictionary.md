# Initial analysis data dictionary

| Source/derived column | Meaning | Notes |
|---|---|---|
| `game-ver` | Game software version | Not necessarily a weapon-balance patch |
| `period` | UTC 2-hour rotation window | Not exact battle start timestamp |
| `mode` | Game mode (`area`, `yagura`, `hoko`, `asari`, `nawabari`) | Some modes are not competitive ranked |
| `stage` | Map identifier | Game-specific internal key |
| `win` | Winning team (`alpha` or `bravo`) | Team A is the uploader's team |
| `A1-weapon` | Uploader weapon | Excluded from initial weapon-pick statistics |
| `A2-weapon`…`B4-weapon` | Other seven player's weapons | Still subject to selection bias |
| `balance_patch` | Reviewed balance era or `UNREVIEWED_*` | Derived from version mapping, used for Parquet partition |
| `game_version` | Partition key from `game-ver` | Exact software version or `unknown` |
| `source_csv` | Source ZIP member path | Provenance; not a unique match ID |

**Important:** one stat.ink CSV row is not guaranteed to be one unique match: the same match may be uploaded by more than one participant.
