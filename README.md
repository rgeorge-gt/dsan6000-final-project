# DSAN 6000 Final Project — Splatoon 3 Competitive Weapon Balance

**Research question:** How does the relationship between weapon selection and match outcomes vary across maps, competitive modes, and balance patches in *Splatoon 3*?

This repository starts with a reproducible, full-archive ingestion and patch-level EDA workflow. Later stages will use Apache Spark and statistical modeling. Raw match records and generated Parquet files are **not** included in GitHub.

## Data sources

- **stat.ink archive:** https://dl-stats.stats.ink/splatoon-3/battle-results-csv/ — `battle-results-csv.zip`, 1.01 GiB compressed on 2026-11-23; archive updates regularly.
- **Documented CSV fields:** https://github.com/fetus-hina/stat.ink/wiki/Spl3-%EF%BC%8D-CSV-Schema-%EF%BC%8D-Battle
- **Nintendo update history (earlier):** https://en-americas-support.nintendo.com/app/answers/detail/a_id/61257/
- **Nintendo update history (recent):** https://en-americas-support.nintendo.com/app/answers/detail/a_id/59461/

The submitted proposal uses **stat.ink**, *not Hugging Face*. This public dataset contains voluntarily uploaded battle results, not the complete population of Splatoon 3 matches.


## What is a “patch” here?

The raw `game-ver` column is the **observed software version**. Because not every software update rebalance weapons, `config/version_to_balance_patch.csv` provides an explicit, reviewable mapping of versions to a balance era. It includes selected **verified 2025–2026 versions** as an initial seed, with `10.0.1` grouped into `10.0.0` (bug-fix release) but `11.0.1` kept separate (Nintendo confirmed unexpected weapon parameter changes).

Older versions absent from the mapping are kept in separate partitions named `balance_patch=UNREVIEWED_<version>`. **These are not true verified balance eras.** They are placeholders until Nintendo's release notes have been reviewed. This is intentional: silently lumping old versions could invalidate later conclusions. Add entries to the mapping before final longitudinal modeling, and rerun the ingest to repartition.

The report includes **official release dates where reviewed**, separately from **first and last observed rotation periods**.

### Important definitions

- **`record_count`:** CSV rows / stat.ink uploads; not deduplicated unique battles. There is no guaranteed global match identifier in this source.
- **`first_observed_period_utc`, `last_observed_period_utc`:** earliest/latest `period` values associated with the records. `period` is a 2-hour stage-rotation window, **not an exact battle timestamp**. These dates are not necessarily Nintendo's release dates.
- **`picks_excluding_A1`:** counts for player slots A2, A3, A4, B1–B4. A1 is the uploader and is intentionally excluded to reduce uploader bias; the entire data sample is still not representative of all matches.
- **`mapping_reviewed`:** whether a version has been manually assigned to a balance era. Absence does not imply no balance changes.

## Tests and publishing

```bash
python -m pytest -q
# Inside your cloned repo, after copying these files:
git add .
git commit -m "Add stat.ink full archive ingestion and patch-centered EDA"
git push origin main
```

Do not `git add` the raw ZIP or generated partition files. `.gitignore` excludes them, but small generated EDA reports and figures are tracked.
