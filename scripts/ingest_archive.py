"""Stream every stat.ink CSV from its ZIP, preserve all columns in patch-partitioned Parquet,
and produce trustworthy first-pass EDA tables without loading the full archive into RAM.

Examples:
  python scripts/ingest_archive.py --limit-files 5 --overwrite    # smoke test
  python scripts/ingest_archive.py --overwrite                  # complete archive
  python scripts/ingest_archive.py --summary-only --overwrite    # counts, no pyarrow

Each CSV row is a *community-uploaded battle record*, not necessarily a unique
real-world match. `period` is a two-hour rotation window, not a precise start time.
"""
from __future__ import annotations

import argparse
import csv
import io
import hashlib
import json
import re
import shutil
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
PLAYER_SLOTS = tuple(f"{team}{number}" for team, numbers in (("A", range(2, 5)), ("B", range(1, 5))) for number in numbers)
CORE_COLUMNS = ("game-ver", "period", "mode", "stage", "win")


def load_patch_map(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Patch mapping CSV not found: {path}")
    df = pd.read_csv(path, dtype=str).fillna("")
    required = {"game_version", "balance_patch"}
    if not required.issubset(df.columns):
        raise ValueError(f"Patch mapping needs columns: {sorted(required)}")
    if df.game_version.duplicated().any():
        raise ValueError("Patch mapping contains duplicate game_version rows")
    return dict(zip(df.game_version, df.balance_patch))


def version_label(value: object) -> str:
    value = str(value).strip()
    return value if VERSION_PATTERN.fullmatch(value) else "unknown"


def patch_for(version: str, mapping: dict[str, str]) -> str:
    # Avoid guessing whether older / newly added software versions change balance.
    return mapping.get(version, f"UNREVIEWED_{version}")


def bump_pair(counter: Counter, frame: pd.DataFrame, a: str, b: str) -> None:
    pairs = frame[[a, b]].fillna("").value_counts(dropna=False)
    for (x, y), count in pairs.items():
        counter[(str(x), str(y))] += int(count)


def write_counter_csv(counter: Counter, columns: list[str], target: Path) -> None:
    rows = [(*key, count) for key, count in sorted(counter.items())]
    pd.DataFrame(rows, columns=[*columns, "record_count"]).to_csv(target, index=False)


def iter_csv_chunks(binary, chunksize: int, member_name: str, quality: Counter, anomaly_samples: list[dict]):
    """Read a CSV member without dropping rows when record widths vary.

    stat.ink historical CSV members can contain records with more/fewer fields
    than their header. Python's csv.reader honors quoted commas/newlines; extra
    fields are preserved as JSON instead of being silently dropped. Field-count
    discrepancies remain prominently flagged for review before analysis.
    """
    if chunksize <= 0:
        raise ValueError("chunksize must be > 0")
    with io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as text_stream:
        reader = csv.reader(text_stream, strict=True)
        header = None
        for cells in reader:
            if not cells or (len(cells) == 1 and (not cells[0].strip() or cells[0].lstrip().startswith("#"))):
                continue
            header = [field.strip() for field in cells]
            break
        if header is None:
            raise ValueError(f"Empty CSV member: {member_name}")
        if len(set(header)) != len(header):
            raise ValueError(f"Duplicate header columns in {member_name}")
        absent = [column for column in CORE_COLUMNS if column not in header]
        if absent:
            raise ValueError(f"{member_name} missing required fields: {absent}")
        audit_cols = ("_csv_overflow_json", "_csv_field_count", "_csv_width_status")
        if any(column in header for column in audit_cols):
            raise ValueError(f"CSV audit column name conflicts with source header: {member_name}")

        expected = len(header)
        batch = []
        for cells in reader:
            if not cells or (len(cells) == 1 and (not cells[0].strip() or cells[0].lstrip().startswith("#"))):
                continue
            actual = len(cells)
            if actual != expected:
                status = "extra" if actual > expected else "short"
                quality[f"csv_rows_{status}_fields"] += 1
                if actual > expected and any(str(v).strip() for v in cells[expected:]):
                    quality["csv_rows_extra_nonempty_fields"] += 1
                if len(anomaly_samples) < 30:
                    anomaly_samples.append({
                        "member": member_name, "physical_end_line": reader.line_num,
                        "header_fields": expected, "row_fields": actual,
                        "last_header_names": header[-5:],
                        "trailing_row_values": [str(value)[:120] for value in cells[-5:]],
                        "extra_values": [str(value)[:120] for value in cells[expected:][:5]],
                    })
            else:
                status = "ok"
            fixed = (cells[:expected] + [""] * max(0, expected - actual))
            batch.append(fixed + [json.dumps(cells[expected:], ensure_ascii=False), str(actual), status])
            if len(batch) >= chunksize:
                yield pd.DataFrame(batch, columns=header + list(audit_cols))
                batch.clear()
        if batch:
            yield pd.DataFrame(batch, columns=header + list(audit_cols))


def process_zip(
    archive_path: Path,
    output_root: Path,
    patch_map_path: Path,
    chunksize: int = 10_000,
    limit_files: int | None = None,
    summary_only: bool = False,
    overwrite: bool = False,
) -> dict:
    if not archive_path.exists():
        raise FileNotFoundError(f"Missing archive: {archive_path}. Run scripts/download_data.py first.")
    if output_root.exists():
        if not overwrite:
            raise FileExistsError(f"Output already exists: {output_root}. Pass --overwrite to rebuild.")
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)
    report_dir = output_root / "reports"
    report_dir.mkdir()
    parquet_root = output_root / "battles"
    if not summary_only:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise ImportError("Parquet output requires pyarrow; run: pip install -r requirements.txt") from exc
        parquet_root.mkdir()

    mapping = load_patch_map(patch_map_path)
    release_info = pd.read_csv(patch_map_path, dtype=str).fillna("")
    release_dates = dict(zip(release_info.game_version, release_info.get("release_date_us", pd.Series([""] * len(release_info)))))
    patch_release_dates = {k: release_dates.get(k, "") for k in set(mapping.values())}
    summary = defaultdict(lambda: {"records": 0, "files": set(), "min_period": None, "max_period": None, "dates": set(), "missing_period": 0, "invalid_outcome": 0})
    by_mode = Counter()
    by_stage = Counter()
    by_weapon = Counter()
    by_weapon_wins = Counter()
    by_file_patch = Counter()
    quality = Counter()
    anomaly_samples: list[dict] = []

    with zipfile.ZipFile(archive_path) as archive:
        members = sorted((x for x in archive.infolist() if x.filename.lower().endswith(".csv") and not x.is_dir()), key=lambda x: x.filename)
        available_files = len(members)
        if limit_files is not None:
            members = members[:limit_files]
        if not members:
            raise ValueError("No CSV member files found")
        print(f"Reading {len(members):,} / {available_files:,} CSV files from ZIP")
        for file_i, member in enumerate(members, start=1):
            # CSVs are UTF-8 with quoted JSON and a few comment lines. Pandas respects quoted commas.
            with archive.open(member, "r") as binary:
                try:
                    reader = iter_csv_chunks(binary, chunksize, member.filename, quality, anomaly_samples)
                    for chunk_i, frame in enumerate(reader):
                        absent = [col for col in CORE_COLUMNS if col not in frame.columns]
                        if absent:
                            raise ValueError(f"{member.filename} missing fields {absent}")
                        quality["records_total"] += len(frame)
                        versions = frame["game-ver"].map(version_label)
                        patches = versions.map(lambda x: patch_for(x, mapping))
                        frame = frame.copy()
                        frame["_game_version"] = versions
                        frame["_balance_patch"] = patches
                        parsed_times = pd.to_datetime(frame["period"], utc=True, errors="coerce")
                        quality["period_missing_or_invalid"] += int(parsed_times.isna().sum())
                        quality["version_missing_or_invalid"] += int(versions.eq("unknown").sum())
                        quality["win_missing_or_invalid"] += int((~frame.win.isin(("alpha", "bravo"))).sum())
                        quality["unreviewed_version_records"] += int(patches.str.startswith("UNREVIEWED_").sum())

                        for (version, patch), sub in frame.groupby(["_game_version", "_balance_patch"], sort=False):
                            key = (str(version), str(patch))
                            stats = summary[key]
                            stats["records"] += len(sub)
                            stats["files"].add(member.filename)
                            dt = parsed_times.loc[sub.index].dropna()
                            stats["missing_period"] += len(sub) - len(dt)
                            stats["invalid_outcome"] += int((~sub.win.isin(("alpha", "bravo"))).sum())
                            if not dt.empty:
                                smallest, largest = dt.min(), dt.max()
                                stats["min_period"] = smallest if stats["min_period"] is None else min(stats["min_period"], smallest)
                                stats["max_period"] = largest if stats["max_period"] is None else max(stats["max_period"], largest)
                                stats["dates"].update(dt.dt.strftime("%Y-%m-%d").unique().tolist())
                            by_file_patch[(member.filename, version, patch)] += len(sub)

                            if not summary_only:
                                folder = parquet_root / f"balance_patch={patch}" / f"game_version={version}"
                                folder.mkdir(parents=True, exist_ok=True)
                                short_hash = hashlib.sha1(member.filename.encode("utf-8")).hexdigest()[:12]
                                destination = folder / f"source-{short_hash}-chunk-{chunk_i:05d}.parquet"
                                # Keep all original CSV columns (as strings) + provenance.
                                # Avoid duplicating Hive partition keys inside the file.
                                payload = sub.drop(columns=["_game_version", "_balance_patch"]).copy()
                                payload["source_csv"] = member.filename
                                pq.write_table(pa.Table.from_pandas(payload.astype("string"), preserve_index=False), destination, compression="zstd")

                        useful = frame.rename(columns={"_balance_patch": "_patch"})
                        bump_pair(by_mode, useful, "_patch", "mode")
                        bump_pair(by_stage, useful, "_patch", "stage")
                        for slot in PLAYER_SLOTS:
                            column = f"{slot}-weapon"
                            if column not in useful.columns:
                                quality[f"missing_column_{column}_records"] += len(useful)
                                continue
                            team = "alpha" if slot[0] == "A" else "bravo"
                            pairs = pd.DataFrame({
                                "patch": useful["_patch"],
                                "weapon": useful[column],
                                "won": useful["win"].eq(team).astype(int),
                            })
                            pairs = pairs.loc[pairs.weapon.ne("")]
                            if pairs.empty:
                                continue
                            counts = pairs.groupby(["patch", "weapon"], dropna=False)["won"].agg(["count", "sum"])
                            for (patch, weapon), rec in counts.iterrows():
                                by_weapon[(str(patch), str(weapon))] += int(rec["count"])
                                by_weapon_wins[(str(patch), str(weapon))] += int(rec["sum"])
                except Exception as exc:
                    raise RuntimeError(f"Failed to read member {member.filename}: {exc}") from exc
            if file_i == 1 or file_i % 100 == 0 or file_i == len(members):
                print(f"Processed CSV files: {file_i:,}/{len(members):,} (records: {quality['records_total']:,})", flush=True)

    overview = []
    for (version, patch), info in sorted(summary.items()):
        first = info["min_period"]
        last = info["max_period"]
        overview.append({
            "game_version": version,
            "balance_patch": patch,
            "official_version_release_date_us": release_dates.get(version, ""),
            "official_balance_patch_release_date_us": patch_release_dates.get(patch, ""),
            "mapping_reviewed": not patch.startswith("UNREVIEWED_"),
            "record_count": info["records"],
            "first_observed_period_utc": first.isoformat() if first is not None else "",
            "last_observed_period_utc": last.isoformat() if last is not None else "",
            "distinct_observed_utc_dates": len(info["dates"]),
            "span_calendar_days": int((last.date() - first.date()).days + 1) if first is not None else "",
            "csv_files_containing_version": len(info["files"]),
            "missing_or_invalid_period_count": info["missing_period"],
            "invalid_winner_count": info["invalid_outcome"],
        })
    patch_df = pd.DataFrame(overview).sort_values(["first_observed_period_utc", "game_version"], na_position="last")
    patch_df.to_csv(report_dir / "version_summary.csv", index=False)
    patch_summary = (
        patch_df.groupby("balance_patch", dropna=False)
        .agg(record_count=("record_count", "sum"),
             official_balance_patch_release_date_us=("official_balance_patch_release_date_us", "first"),
             first_observed_period_utc=("first_observed_period_utc", "min"),
             last_observed_period_utc=("last_observed_period_utc", "max"),
             game_versions=("game_version", lambda items: ", ".join(items)))
        .reset_index()
        .sort_values("record_count", ascending=False)
    )
    patch_summary.to_csv(report_dir / "balance_patch_summary.csv", index=False)
    write_counter_csv(by_mode, ["balance_patch", "mode"], report_dir / "patch_mode_counts.csv")
    write_counter_csv(by_stage, ["balance_patch", "stage"], report_dir / "patch_stage_counts.csv")
    write_counter_csv(by_file_patch, ["archive_csv", "game_version", "balance_patch"], report_dir / "source_file_patch_counts.csv")

    weapon_rows = [
        {"balance_patch": patch, "weapon": weapon, "picks_excluding_A1": count,
         "wins_excluding_A1": by_weapon_wins[(patch, weapon)],
         "observed_pick_win_rate": by_weapon_wins[(patch, weapon)] / count}
        for (patch, weapon), count in by_weapon.items() if count
    ]
    pd.DataFrame(weapon_rows, columns=["balance_patch", "weapon", "picks_excluding_A1", "wins_excluding_A1", "observed_pick_win_rate"]).sort_values(
        ["balance_patch", "picks_excluding_A1"], ascending=[True, False]
    ).to_csv(report_dir / "patch_weapon_counts.csv", index=False)

    (report_dir / "csv_width_anomaly_samples.json").write_text(json.dumps(anomaly_samples, indent=2, ensure_ascii=False), encoding="utf-8")
    metadata = {
        "source": "stat.ink community-submitted Splatoon 3 battle CSV archive",
        "source_archive": str(archive_path),
        "archive_files_available": available_files,
        "archive_files_processed": len(members),
        "run_is_complete_archive": limit_files is None,
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "parquet_written": not summary_only,
        "counts_are": "battle uploads / CSV rows, NOT deduplicated unique actual matches",
        "date_semantics": "UTC two-hour rotation window, NOT exact battle start time",
        "patch_mapping": "Only reviewed versions grouped; UNREVIEWED_ versions remain separate",
        "weapon_counts": "seven non-uploader slots A2-A4 and B1-B4; picks may duplicate games",
        "csv_width_anomaly_sample_file": "csv_width_anomaly_samples.json",
        "csv_width_discrepancy_warning": "Inspect anomalous rows before interpreting patch counts: extra fields are preserved but may indicate shifts, not merely trailing columns.",
        "quality": dict(quality),
    }
    (report_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    if quality["csv_rows_extra_fields"] or quality["csv_rows_short_fields"]:
        print("WARNING: CSV field-count anomalies detected. Check reports/csv_width_anomaly_samples.json and run_metadata.json before relying on results.")
    print(f"Reports saved to {report_dir}")
    print(patch_df[["game_version", "balance_patch", "record_count", "first_observed_period_utc", "last_observed_period_utc"]].to_string(index=False))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/raw/battle-results-csv.zip"))
    parser.add_argument("--output", type=Path, default=Path("data/processed"))
    parser.add_argument("--patch-map", type=Path, default=Path("config/version_to_balance_patch.csv"))
    parser.add_argument("--chunksize", type=int, default=10_000)
    parser.add_argument("--limit-files", type=int, default=None, help="Smoke test on first N daily CSVs")
    parser.add_argument("--summary-only", action="store_true", help="Summaries without writing Parquet")
    parser.add_argument("--overwrite", action="store_true", help="Rebuild output directory")
    args = parser.parse_args()
    process_zip(args.archive, args.output, args.patch_map, args.chunksize, args.limit_files, args.summary_only, args.overwrite)


if __name__ == "__main__":
    main()
