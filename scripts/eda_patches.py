"""Create a patch-centered EDA summary and plots from the ingestion reports.

Run: python scripts/eda_patches.py
This step uses compact CSV aggregates; it does not re-read the multi-GB source.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def version_key(v: object) -> tuple[int, int, int]:
    chunks = re.findall(r"\d+", str(v))[:3]
    return tuple(int(n) for n in (chunks + ["0"] * 3)[:3])


def run_eda(report_dir: Path, output_dir: Path, top_n_weapons: int = 20) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    versions = pd.read_csv(report_dir / "version_summary.csv", keep_default_na=False)
    patches = pd.read_csv(report_dir / "balance_patch_summary.csv", keep_default_na=False)
    modes = pd.read_csv(report_dir / "patch_mode_counts.csv", keep_default_na=False)
    weapons = pd.read_csv(report_dir / "patch_weapon_counts.csv", keep_default_na=False)
    meta = json.loads((report_dir / "run_metadata.json").read_text())

    if versions.empty:
        raise ValueError("No versions found in output. Check source archive and ingestion.")
    versions = versions.sort_values("game_version", key=lambda series: series.map(version_key))
    fig, ax = plt.subplots(figsize=(12, max(5, len(versions) * .29)))
    ax.barh(versions["game_version"], versions["record_count"])
    ax.set_xlabel("Community-uploaded battle records (not distinct matches)")
    ax.set_ylabel("Software version (game-ver)")
    ax.set_title("Splatoon 3: Recorded battles by software version")
    ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(output_dir / "version_record_counts.png", dpi=160)
    plt.close(fig)

    for_report = versions.sort_values("record_count", ascending=False)
    total = int(versions.record_count.sum())
    complete = meta.get("run_is_complete_archive", False)
    incomplete_flag = "COMPLETE archive" if complete else "PARTIAL SAMPLE (do not treat as archive totals)"
    reported = pd.to_datetime(versions.first_observed_period_utc, utc=True, errors="coerce")
    latest = pd.to_datetime(versions.last_observed_period_utc, utc=True, errors="coerce")
    first = reported.min()
    last = latest.max()

    if not modes.empty:
        for_mode = modes.pivot_table(index="balance_patch", columns="mode", values="record_count", aggfunc="sum", fill_value=0)
        for_mode = for_mode.loc[sorted(for_mode.index, key=version_key)]
        fig, ax = plt.subplots(figsize=(12, max(5, len(for_mode) * .37)))
        for_mode.plot.barh(stacked=True, ax=ax, legend=True)
        ax.set_xlabel("Uploaded battle records")
        ax.set_ylabel("Reviewed balance era or unreviewed version")
        ax.set_title("Record coverage across game modes by balance era")
        ax.invert_yaxis()
        ax.legend(title="Mode", loc="center left", bbox_to_anchor=(1.0, .5))
        fig.tight_layout()
        fig.savefig(output_dir / "balance_patch_mode_counts.png", dpi=160)
        plt.close(fig)

    if not weapons.empty:
        popular = (weapons.groupby("weapon", as_index=False)["picks_excluding_A1"].sum()
                   .sort_values("picks_excluding_A1", ascending=False).head(top_n_weapons))
        fig, ax = plt.subplots(figsize=(11, 7))
        ax.barh(popular["weapon"], popular["picks_excluding_A1"])
        ax.invert_yaxis()
        ax.set_xlabel("Observed weapon picks excluding uploader A1")
        ax.set_title("Most observed weapons (seven non-uploader slots)")
        fig.tight_layout()
        fig.savefig(output_dir / "most_used_weapons.png", dpi=160)
        plt.close(fig)

    def md_table(df: pd.DataFrame, fields: list[str]) -> str:
        lines = ["| " + " | ".join(fields) + " |", "| " + " | ".join(["---"] * len(fields)) + " |"]
        for _, rec in df[fields].iterrows():
            lines.append("| " + " | ".join(str(rec[k]).replace("|", "\\|") for k in fields) + " |")
        return "\n".join(lines)

    text = f"""# Initial EDA — Splatoon 3 game-version and balance-patch coverage

**Run type:** {incomplete_flag}  
**Records in analysis:** {total:,}  
**Distinct software versions:** {len(versions)}  
**Reviewed balance-patch periods with at least one version in data:** {int(patches[~patches.balance_patch.str.startswith('UNREVIEWED_')].shape[0])}  
**Earliest observed rotation:** {first if pd.notna(first) else 'N/A'}  
**Latest observed rotation:** {last if pd.notna(last) else 'N/A'}

## Key definitions

- A *record* is a stat.ink community-uploaded battle entry. Multiple uploads may describe the same physical battle; totals are not deduplicated Nintendo matches.
- `game-ver` is the recorded software version. It is not synonymous with a balance patch.
- `balance_patch` is curated using official release notes. Unknown versions are labeled `UNREVIEWED_*`, **not** guessed or automatically merged.
- `period` is a 2-hour stage rotation window in UTC, not the exact time the match began. Min/max refer to observed data coverage, **not** official release or sunset dates.
- Non-uploader weapon counts exclude A1, per the stat.ink schema warning; selection bias remains.

## Records and observed date range by software version

{md_table(versions, ['game_version', 'balance_patch', 'record_count', 'official_version_release_date_us', 'first_observed_period_utc', 'last_observed_period_utc', 'distinct_observed_utc_dates'])}

## Records by reviewed balance-patch era / provisional version

{md_table(patches, ['balance_patch', 'game_versions', 'record_count', 'official_balance_patch_release_date_us', 'first_observed_period_utc', 'last_observed_period_utc'])}

## Key charts

![Version record counts](version_record_counts.png)

![Mode counts by patch](balance_patch_mode_counts.png)

![Most observed weapons](most_used_weapons.png)

## Data-quality checks

```json
{json.dumps(meta.get('quality', {}), indent=2)}
```

## Limitations / next work

1. Review and extend `config/version_to_balance_patch.csv` for older versions before cross-patch balance conclusions.
2. Review distribution of observation counts per patch and mode before modeling. Sparse periods should be pooled only when substantively justified.
3. `period` covers 2-hour rotations: do not use it to claim exact game-start timestamps.
4. Volunteer samples are nonrandom and uploads may duplicate a single match. Calculated 'win rates' describe observed picks only; they are not causal weapon-strength estimates.
5. The parquet extraction preserves all original columns for later Spark analysis; move it to S3 or process in Spark when starting the distributed processing milestone.

**Sources:** stat.ink CSV schema: https://github.com/fetus-hina/stat.ink/wiki/Spl3-%EF%BC%8D-CSV-Schema-%EF%BC%8D-Battle  
Nintendo update history: https://en-americas-support.nintendo.com/app/answers/detail/a_id/61257/  
Current Nintendo releases: https://en-americas-support.nintendo.com/app/answers/detail/a_id/59461/
"""
    destination = output_dir / "eda_report.md"
    destination.write_text(text, encoding="utf-8")
    print(f"{incomplete_flag} — {total:,} uploaded records, {len(versions)} versions")
    print("\nRecords and UTC rotation ranges by version:")
    print(versions[["game_version", "balance_patch", "record_count", "first_observed_period_utc", "last_observed_period_utc"]].to_string(index=False))
    print(f"\nWrote {destination} and charts in {output_dir}")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, default=Path("data/processed/reports"))
    parser.add_argument("--out", type=Path, default=Path("reports/eda"))
    args = parser.parse_args()
    run_eda(args.reports, args.out)


if __name__ == "__main__":
    main()
