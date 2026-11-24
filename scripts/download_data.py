"""Download the complete, periodically updated stat.ink Splatoon 3 CSV archive.

Run: python scripts/download_data.py
The download is about 1 GiB compressed (as of 2026-11-23).
"""
#Get packages
from __future__ import annotations
import argparse
import hashlib
import shutil
import time
import zipfile
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL = "https://dl-stats.stats.ink/splatoon-3/battle-results-csv/battle-results-csv.zip"


def get_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=5, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({"User-Agent": "dsan6000-splatoon3-academic-project/1.0"})
    return session


def download(url: str, output: Path, expected_sha256: str | None = None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    if output.is_file():
        print(f"Archive already exists: {output}")
        print("To refresh, delete it and rerun. The source archive is updated regularly.")
    else:
        session = get_session()
        previous = partial.stat().st_size if partial.exists() else 0
        headers = {"Range": f"bytes={previous}-"} if previous else {}
        with session.get(url, headers=headers, stream=True, timeout=(20, 120)) as response:
            response.raise_for_status()
            is_partial = response.status_code == 206
            if previous and not is_partial:
                print("Server did not accept resume request; restarting download.")
            mode = "ab" if is_partial else "wb"
            transferred = previous if is_partial else 0
            total = response.headers.get("Content-Length")
            total = (int(total) + transferred) if total else None
            start = time.monotonic()
            last_update = start
            with partial.open(mode) as handle:
                for data in response.iter_content(chunk_size=2 * 1024 * 1024):
                    if not data:
                        continue
                    handle.write(data)
                    transferred += len(data)
                    now = time.monotonic()
                    if now - last_update >= 5:
                        suffix = f" of {total / 2**20:,.0f} MiB" if total else ""
                        print(f"Downloaded {transferred / 2**20:,.0f} MiB{suffix}", flush=True)
                        last_update = now
        partial.rename(output)
        print(f"Downloaded to {output} ({output.stat().st_size / 2**30:.2f} GiB)")

    if expected_sha256:
        digest = hashlib.sha256()
        with output.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(block)
        actual = digest.hexdigest()
        if actual.lower() != expected_sha256.lower():
            raise ValueError(f"SHA-256 mismatch: expected {expected_sha256}, got {actual}")
        print(f"SHA-256 verified: {actual}")

    with zipfile.ZipFile(output) as archive:
        csv_count = sum(info.filename.lower().endswith(".csv") for info in archive.infolist())
        if not csv_count:
            raise ValueError("Archive ZIP contains no CSV files")
        print(f"ZIP opened successfully: {csv_count:,} CSV members")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--output", type=Path, default=Path("data/raw/battle-results-csv.zip"))
    parser.add_argument("--sha256", help="Optional digest from current download index; changes when archive updates")
    args = parser.parse_args()
    download(args.url, args.output, args.sha256)


if __name__ == "__main__":
    main()
