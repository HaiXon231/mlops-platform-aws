"""Move the fixed monthly inputs between the local cache and persistent S3."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from training.config import load_config
from training.data import inspect_month, month_path


PREFIX = "yellow-taxi"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def keys(period: str) -> tuple[str, str]:
    stem = f"{PREFIX}/{period}/yellow_tripdata_{period}"
    return f"{stem}.parquet", f"{stem}.metadata.json"


def _head_or_none(s3, bucket: str, key: str):
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def sync_months(s3, bucket: str, config) -> list[dict]:
    """Preserve provenance and refuse to replace a different remote dataset."""
    result = []
    for period in config.all_months:
        metadata = inspect_month(config, period)
        path = month_path(config.data_dir, period)
        sidecar = path.with_suffix(".metadata.json")
        if not sidecar.exists():
            sidecar.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            saved = json.loads(sidecar.read_text(encoding="utf-8"))
            if bool(saved.get("source_verified")) != metadata["source_verified"]:
                raise ValueError(f"Source verification state changed for {period}")
        data_key, metadata_key = keys(period)
        existing = _head_or_none(s3, bucket, data_key)
        if existing and existing.get("Metadata", {}).get("sha256") != metadata["sha256"]:
            raise ValueError(f"S3 object {data_key} exists with a different SHA256; review it manually")
        if existing is None:
            s3.upload_file(
                str(path), bucket, data_key,
                ExtraArgs={"Metadata": {"sha256": metadata["sha256"], "source-verified": str(metadata["source_verified"]).lower()}},
            )
        if _head_or_none(s3, bucket, metadata_key) is None:
            s3.upload_file(str(sidecar), bucket, metadata_key, ExtraArgs={"ContentType": "application/json"})
        else:
            with s3.get_object(Bucket=bucket, Key=metadata_key)["Body"] as body:
                remote_source = json.loads(body.read())
            local_source = json.loads(sidecar.read_text(encoding="utf-8"))
            for field in ("sha256", "source_url", "source_verified"):
                if remote_source.get(field) != local_source.get(field):
                    raise ValueError(f"S3 source metadata differs for {period}: {field}")
        verified = s3.head_object(Bucket=bucket, Key=data_key)
        if verified["ContentLength"] != path.stat().st_size or verified.get("Metadata", {}).get("sha256") != metadata["sha256"]:
            raise ValueError(f"S3 HEAD verification failed for {data_key}")
        result.append({"period": period, "key": data_key, "sha256": metadata["sha256"], "source_verified": metadata["source_verified"]})
    return result


def fetch_months(s3, bucket: str, config) -> list[dict]:
    """Download to a temporary directory before M2.1 verifies Parquet and SHA256."""
    config.data_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for period in config.all_months:
        data_key, metadata_key = keys(period)
        path = month_path(config.data_dir, period)
        sidecar = path.with_suffix(".metadata.json")
        s3.download_file(bucket, metadata_key, str(sidecar))
        source = json.loads(sidecar.read_text(encoding="utf-8"))
        if source.get("period") != period or not isinstance(source.get("source_verified"), bool):
            raise ValueError(f"Invalid source metadata for {period}")
        remote = s3.head_object(Bucket=bucket, Key=data_key)
        if remote.get("Metadata", {}).get("sha256") != source.get("sha256"):
            raise ValueError(f"S3 metadata checksum differs from sidecar for {period}")
        if not path.exists() or sha256(path) != source["sha256"]:
            partial = path.with_suffix(".part")
            try:
                s3.download_file(bucket, data_key, str(partial))
                if sha256(partial) != source["sha256"]:
                    raise ValueError(f"Downloaded SHA256 mismatch for {period}")
                partial.replace(path)
            finally:
                partial.unlink(missing_ok=True)
        metadata = inspect_month(config, period)
        result.append({"period": period, "sha256": metadata["sha256"], "source_verified": metadata["source_verified"]})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync fixed training Parquet files with persistent S3")
    parser.add_argument("action", choices=("sync", "fetch"))
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--config", type=Path, default=Path("configs/training.yaml"))
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--profile", help="Local AWS profile; omit in EKS to use Pod Identity")
    args = parser.parse_args()
    config = load_config(args.config).with_paths(data_dir=args.data_dir)
    s3 = boto3.Session(profile_name=args.profile).client("s3")
    try:
        result = sync_months(s3, args.bucket, config) if args.action == "sync" else fetch_months(s3, args.bucket, config)
    except (BotoCoreError, ClientError, ValueError, FileNotFoundError) as error:
        print(f"Dataset S3 {args.action} failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
