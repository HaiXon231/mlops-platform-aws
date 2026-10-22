"""Back up MLflow PostgreSQL to S3 and verify restored Registry metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError


TABLES = ("experiments", "runs", "registered_models", "model_versions", "registered_model_aliases")


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()


def pg(*args: str, database: str | None = None) -> str:
    return command(*args, "-h", os.environ["PGHOST"], "-U", os.environ["PGUSER"], "-d", database or os.environ["PGDATABASE"])


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def metadata(database: str) -> dict:
    counts = {table: int(pg("psql", "-At", "-c", f"select count(*) from {table}", database=database)) for table in TABLES}
    run_ids = json.loads(pg(
        "psql", "-At", "-c",
        "select coalesce(json_agg(run_uuid order by run_uuid), '[]'::json) from runs",
        database=database,
    ))
    versions = json.loads(pg(
        "psql", "-At", "-c",
        "select coalesce(json_agg(row_to_json(t) order by t.name, t.version), '[]'::json) from "
        "(select name, version, run_id from model_versions) t",
        database=database,
    ))
    aliases = json.loads(pg(
        "psql", "-At", "-c",
        "select coalesce(json_agg(row_to_json(t) order by t.name, t.alias), '[]'::json) from "
        "(select name, alias, version from registered_model_aliases) t",
        database=database,
    ))
    return {"counts": counts, "run_ids": run_ids, "model_versions": versions, "aliases": aliases}


def restore_temp(dump: Path) -> dict:
    name = f"mlops_restore_check_{os.getpid()}"
    command("createdb", "-h", os.environ["PGHOST"], "-U", os.environ["PGUSER"], name)
    try:
        pg("pg_restore", "--no-owner", "--no-acl", str(dump), database=name)
        return metadata(name)
    finally:
        command("dropdb", "-h", os.environ["PGHOST"], "-U", os.environ["PGUSER"], "--if-exists", name)


def backup(s3, bucket: str, dump: Path) -> str:
    pg("pg_dump", "-Fc", "-f", str(dump))
    command("pg_restore", "--list", str(dump))
    snapshot = restore_temp(dump)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    key = f"postgres/{stamp}.dump"
    checksum = digest(dump)
    info = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": os.environ["PGDATABASE"],
        "dump_key": key,
        "sha256": checksum,
        "size_bytes": dump.stat().st_size,
        "snapshot": snapshot,
    }
    s3.upload_file(str(dump), bucket, key, ExtraArgs={"Metadata": {"sha256": checksum}})
    s3.put_object(Bucket=bucket, Key=f"{key}.json", Body=(json.dumps(info, sort_keys=True) + "\n").encode(), ContentType="application/json")
    head = s3.head_object(Bucket=bucket, Key=key)
    manifest_head = s3.head_object(Bucket=bucket, Key=f"{key}.json")
    if head["ContentLength"] != info["size_bytes"] or head.get("Metadata", {}).get("sha256") != checksum or manifest_head["ContentLength"] < 1:
        raise ValueError("S3 backup verification failed")
    print(f"BACKUP_KEY={key}", flush=True)
    return key


def fetch(s3, bucket: str, key: str, dump: Path) -> dict:
    if not key.startswith("postgres/") or not key.endswith(".dump") or ".." in key:
        raise ValueError("Backup key must be postgres/<name>.dump")
    with s3.get_object(Bucket=bucket, Key=f"{key}.json")["Body"] as body:
        info = json.loads(body.read())
    if info["dump_key"] != key or info["database"] != os.environ["PGDATABASE"]:
        raise ValueError("Backup manifest does not match requested key/database")
    s3.download_file(bucket, key, str(dump))
    if digest(dump) != info["sha256"] or dump.stat().st_size != info["size_bytes"]:
        raise ValueError("Downloaded backup checksum or size mismatch")
    command("pg_restore", "--list", str(dump))
    return info


def restore_primary(dump: Path, expected: dict) -> None:
    database = os.environ["PGDATABASE"]
    table_count = int(pg("psql", "-At", "-c", "select count(*) from information_schema.tables where table_schema='public'"))
    if table_count:
        raise ValueError("Primary database is not empty; refusing to overwrite Registry metadata")
    pg("pg_restore", "--no-owner", "--no-acl", str(dump))
    if metadata(database) != expected:
        raise ValueError("Restored primary database metadata differs from backup manifest")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("backup", "verify", "restore"))
    parser.add_argument("--key", default="")
    args = parser.parse_args()
    bucket = os.environ["BACKUP_BUCKET"]
    s3 = boto3.client("s3")
    try:
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "mlflow.dump"
            if args.mode == "backup":
                backup(s3, bucket, dump)
            else:
                if not args.key:
                    raise ValueError("--key is required for verify/restore")
                info = fetch(s3, bucket, args.key, dump)
                if args.mode == "verify":
                    restored = restore_temp(dump)
                    if restored != info["snapshot"]:
                        raise ValueError("Temporary restore has different run/model-version metadata")
                    print(json.dumps({"status": "verified", "key": args.key, "snapshot": restored}, sort_keys=True))
                else:
                    restore_primary(dump, info["snapshot"])
                    print(json.dumps({"status": "restored", "key": args.key, "snapshot": info["snapshot"]}, sort_keys=True))
    except (BotoCoreError, ClientError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print(f"PostgreSQL backup {args.mode} failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
