"""Official TLC download/cache and bounded-memory monthly sampling."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from training.config import TrainingConfig
from training.features import RAW_COLUMNS, clean_month_batch


def month_path(data_dir: Path, period: str) -> Path:
    return data_dir / f"yellow_tripdata_{period}.parquet"


def source_url(config: TrainingConfig, period: str) -> str:
    return f"{config.source_base_url}/yellow_tripdata_{period}.parquet"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_month(config: TrainingConfig, period: str) -> dict[str, Any]:
    path = month_path(config.data_dir, period)
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"Missing or empty Parquet file: {path}; run data-download")
    parquet = pq.ParquetFile(path)
    missing = set(RAW_COLUMNS) - set(parquet.schema_arrow.names)
    if missing:
        raise ValueError(f"{path} lacks required columns: {', '.join(sorted(missing))}")
    if parquet.metadata.num_rows < 1:
        raise ValueError(f"{path} contains no rows")
    digest = _sha256(path)
    metadata_path = path.with_suffix(".metadata.json")
    saved = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else None
    if saved and saved.get("sha256") != digest:
        raise ValueError(f"SHA256 mismatch for {path}; inspect the cache instead of silently replacing it")
    if saved and saved.get("source_url") != source_url(config, period):
        raise ValueError(f"Source URL mismatch in {metadata_path}")
    return {
        "period": period,
        "path": str(path.resolve()),
        "source_url": source_url(config, period),
        "source_verified": bool(saved and saved.get("source_verified")),
        "sha256": digest,
        "size_bytes": path.stat().st_size,
        "parquet_rows": parquet.metadata.num_rows,
        "downloaded_at_utc": saved.get("downloaded_at_utc") if saved else None,
    }


def download_month(config: TrainingConfig, period: str, *, retries: int = 3) -> dict[str, Any]:
    """Use a verified cache or atomically download one official monthly file."""
    path = month_path(config.data_dir, period)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        metadata = inspect_month(config, period)
        sidecar = path.with_suffix(".metadata.json")
        if not sidecar.exists():
            sidecar.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return metadata

    url = source_url(config, period)
    last_error: Exception | None = None
    for attempt in range(retries):
        temp_path: Path | None = None
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "mlops-platform-training/0.1"})
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status != 200:
                    raise ValueError(f"Download returned HTTP {response.status}: {url}")
                with tempfile.NamedTemporaryFile(dir=config.data_dir, suffix=".part", delete=False) as output:
                    temp_path = Path(output.name)
                    while block := response.read(1024 * 1024):
                        output.write(block)
            parquet = pq.ParquetFile(temp_path)
            missing = set(RAW_COLUMNS) - set(parquet.schema_arrow.names)
            if missing or parquet.metadata.num_rows < 1:
                raise ValueError(f"Downloaded Parquet has missing columns {sorted(missing)} or zero rows: {url}")
            digest = _sha256(temp_path)
            size = temp_path.stat().st_size
            os.replace(temp_path, path)
            metadata = {
                "period": period,
                "path": str(path.resolve()),
                "source_url": url,
                "source_verified": True,
                "sha256": digest,
                "size_bytes": size,
                "parquet_rows": parquet.metadata.num_rows,
                "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            path.with_suffix(".metadata.json").write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return metadata
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"Could not download {url} after {retries} attempts: {last_error}") from last_error


def sample_month(config: TrainingConfig, period: str) -> tuple[pd.DataFrame, dict[str, int]]:
    """Uniformly sample valid rows across the full month using random priorities."""
    path = month_path(config.data_dir, period)
    parquet = pq.ParquetFile(path)
    year, month = (int(part) for part in period.split("-"))
    rng = np.random.default_rng(np.random.SeedSequence([config.seed, year, month]))
    kept: pd.DataFrame | None = None
    seen, valid_rows = 0, 0

    for batch in parquet.iter_batches(batch_size=config.parquet_batch_size, columns=list(RAW_COLUMNS)):
        frame = batch.to_pandas()
        seen += len(frame)
        clean = clean_month_batch(
            frame,
            period,
            config.duration_min_minutes,
            config.duration_max_minutes,
        )
        if clean.empty:
            continue
        count = len(clean)
        clean["_priority"] = rng.random(count)
        clean["_ordinal"] = np.arange(valid_rows, valid_rows + count, dtype="int64")
        valid_rows += count
        candidates = clean if kept is None else pd.concat([kept, clean], ignore_index=True)
        kept = candidates.nsmallest(config.sample_limit_per_month, ["_priority", "_ordinal"])

    if kept is None or kept.empty:
        raise ValueError(f"No valid rows in {path} after cleaning")
    sampled = kept.sort_values("_ordinal").drop(columns=["_priority", "_ordinal"]).reset_index(drop=True)
    return sampled, {
        "rows_before_cleaning": seen,
        "rows_after_cleaning": valid_rows,
        "rows_after_sampling": len(sampled),
    }
