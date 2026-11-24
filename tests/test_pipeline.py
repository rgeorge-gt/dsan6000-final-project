"""Fully offline fixture tests for ZIP ingestion and version-to-balance-era labeling."""
from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ingest_archive import load_patch_map, patch_for, process_zip, version_label
from eda_patches import run_eda


def _csv(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=[
        "season", "period", "game-ver", "lobby", "mode", "stage", "win", "A1-weapon",
        "A2-weapon", "A3-weapon", "A4-weapon", "B1-weapon", "B2-weapon", "B3-weapon", "B4-weapon",
        "A1-abilities", "medal1-name",
    ])
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def test_patch_mapping_is_conservative(tmp_path: Path) -> None:
    mapping = tmp_path / "mapping.csv"
    mapping.write_text("game_version,balance_patch\n10.0.0,10.0.0\n10.0.1,10.0.0\n11.0.1,11.0.1\n")
    d = load_patch_map(mapping)
    assert patch_for("10.0.1", d) == "10.0.0"
    assert patch_for("11.0.1", d) == "11.0.1"
    assert patch_for("8.1.0", d) == "UNREVIEWED_8.1.0"
    assert version_label("bad/value") == "unknown"


def test_archive_eda_and_utc_window(tmp_path: Path) -> None:
    archive = tmp_path / "raw.zip"
    patchmap = tmp_path / "mapping.csv"
    patchmap.write_text("game_version,balance_patch\n10.0.0,10.0.0\n10.0.1,10.0.0\n11.0.1,11.0.1\n")
    default = {
        "season": "Sizzle Season 2025", "lobby": "bankara_open", "mode": "area", "stage": "masaba",
        "win": "alpha", "A1-weapon": "uploader", "A2-weapon": "wakaba", "A3-weapon": "splatana",
        "A4-weapon": "wakaba", "B1-weapon": "buki_a", "B2-weapon": "buki_b",
        "B3-weapon": "buki_a", "B4-weapon": "buki_c", "A1-abilities": '{"some_ability": 1.6}',
        "medal1-name": "#1 Overall Splatter",
    }
    def row(**kw):
        return {**default, **kw}
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("battle-results-csv/2025/06/2025-06-13.csv", "# example CSV comment\n" + _csv([
            row(**{"period": "2025-06-12T22:00:00+00:00", "game-ver": "10.0.0"}),
            row(**{"period": "2025-06-13T00:00:00+00:00", "game-ver": "10.0.0", "mode": "hoko", "win": "bravo"}),
        ]))
        z.writestr("battle-results-csv/2025/07/2025-07-01.csv", _csv([
            row(**{"period": "2025-06-30T22:00:00+00:00", "game-ver": "10.0.1"}),
            row(**{"period": "2025-07-01T00:00:00+00:00", "game-ver": "9.9.9", "mode": "area"}),
        ]))
    out = tmp_path / "processed"
    metadata = process_zip(archive, out, patchmap, chunksize=1, summary_only=True)
    assert metadata["quality"]["records_total"] == 4
    assert metadata["quality"]["unreviewed_version_records"] == 1
    versions = pd.read_csv(out / "reports/version_summary.csv")
    assert versions.set_index("game_version").loc["10.0.0", "record_count"] == 2
    assert versions.set_index("game_version").loc["10.0.1", "record_count"] == 1
    assert versions.set_index("game_version").loc["10.0.0", "first_observed_period_utc"] == "2025-06-12T22:00:00+00:00"
    patches = pd.read_csv(out / "reports/balance_patch_summary.csv")
    assert patches.set_index("balance_patch").loc["10.0.0", "record_count"] == 3
    assert patches.set_index("balance_patch").loc["UNREVIEWED_9.9.9", "record_count"] == 1
    weapons = pd.read_csv(out / "reports/patch_weapon_counts.csv")
    assert not weapons.weapon.eq("uploader").any()  # A1 removed
    assert weapons.loc[(weapons.balance_patch == "10.0.0") & (weapons.weapon == "wakaba"), "picks_excluding_A1"].item() == 6
    assert weapons.loc[(weapons.balance_patch == "10.0.0") & (weapons.weapon == "wakaba"), "wins_excluding_A1"].item() == 4
    assert run_eda(out / "reports", tmp_path / "eda").exists()
    assert (tmp_path / "eda/version_record_counts.png").exists()
    assert "PARTIAL" not in (tmp_path / "eda/eda_report.md").read_text()


def test_partial_archive_marked(tmp_path: Path) -> None:
    archive = tmp_path / "raw.zip"
    mapping = tmp_path / "mapping.csv"
    mapping.write_text("game_version,balance_patch\n11.0.1,11.0.1\n")
    data = "game-ver,period,win,mode,stage\n11.0.1,2026-02-05T00:00:00+00:00,alpha,area,masaba\n"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("2026/02/first.csv", data)
        z.writestr("2026/02/second.csv", data)
    meta = process_zip(archive, tmp_path / "processed", mapping, limit_files=1, summary_only=True)
    assert meta["archive_files_processed"] == 1
    assert not meta["run_is_complete_archive"]


def test_irregular_row_width_preserves_records_and_extra_fields(tmp_path: Path) -> None:
    """Regression: older archive CSV has 87 fields in header but 89 on some rows."""
    mapping = tmp_path / "mapping.csv"
    mapping.write_text("game_version,balance_patch\n1.0.0,1.0.0\n", encoding="utf-8")
    headers = ["game-ver", "period", "mode", "stage", "win"] + [f"f{i}" for i in range(82)]
    base = ["1.0.0", "2022-09-26T00:00:00+00:00", "area", "masaba", "alpha"] + ["" for _ in range(82)]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerow(base)
    writer.writerow(base + ["extra-field-a", "extra-field-b"])
    writer.writerow(base[:-1])
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as zipout:
        zipout.writestr("battle-results-csv/2022-09-26.csv", buf.getvalue())
    out = tmp_path / "processed"
    metadata = process_zip(archive, out, mapping, chunksize=2, summary_only=True)
    assert metadata["quality"]["records_total"] == 3
    assert metadata["quality"]["csv_rows_extra_fields"] == 1
    assert metadata["quality"]["csv_rows_short_fields"] == 1
    anomaly = json.loads((out / "reports/csv_width_anomaly_samples.json").read_text())
    assert len(anomaly) == 2
    assert anomaly[0]["row_fields"] == 89
    versions = pd.read_csv(out / "reports/version_summary.csv")
    assert versions.loc[0, "record_count"] == 3
    from collections import Counter
    from ingest_archive import iter_csv_chunks
    audit = Counter()
    with zipfile.ZipFile(archive) as zipin, zipin.open("battle-results-csv/2022-09-26.csv") as binary:
        frames = list(iter_csv_chunks(binary, 2, "2022-09-26.csv", audit, []))
    assert sum(len(frame) for frame in frames) == 3
    assert any("extra-field-b" in cell for frame in frames for cell in frame["_csv_overflow_json"])
