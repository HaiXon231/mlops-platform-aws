"""Transfer an approved, checksummed M3.1 release through the existing S3 bucket."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from inference.bundle import file_hash, validate_release
from inference.export import export_model

RELEASE_ROOT = "releases/m32"


def release_prefix(name: str, version: str, release_id: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", name):
        raise ValueError("Invalid model name")
    if not version.isdecimal() or int(version) < 1:
        raise ValueError("Specify a positive model version")
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", release_id):
        raise ValueError("Invalid release ID")
    return f"{RELEASE_ROOT}/{name}/v{version}/{release_id}"


def _read_json(s3, bucket: str, key: str) -> dict | None:
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
            return None
        raise
    with response["Body"] as body:
        return json.loads(body.read())


def export_to_s3(bucket: str, name: str, version: str, release_id: str, serving_git_sha: str,
                 tracking_uri: str, s3=None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", serving_git_sha):
        raise ValueError("Serving Git SHA must be a full commit hash")
    prefix = release_prefix(name, version, release_id)
    s3 = s3 or boto3.client("s3")
    completion_key = f"{prefix}/complete.json"
    existing = _read_json(s3, bucket, completion_key)
    if existing is not None:
        if (existing.get("model_name"), existing.get("model_version"), existing.get("serving_git_sha")) != (name, version, serving_git_sha):
            raise ValueError("Release ID already belongs to another source")
        return existing
    with tempfile.TemporaryDirectory(prefix="m32-export-") as temp:
        root = Path(temp) / "release"
        metadata = export_model(tracking_uri, name, version, root)
        files = {str(path.relative_to(root)): file_hash(path) for path in sorted(root.rglob("*")) if path.is_file()}
        for relative in files:
            s3.upload_file(str(root / relative), bucket, f"{prefix}/bundle/{relative}")
        completion = {
            "format_version": 1,
            "release_id": release_id,
            "model_name": name,
            "model_version": version,
            "mlflow_run_id": metadata["mlflow_run_id"],
            "source_run_id": metadata["source_run_id"],
            "training_git_sha": metadata["git_sha"],
            "serving_git_sha": serving_git_sha,
            "artifact_checksum": metadata["artifact_checksum"],
            "files": files,
        }
        try:
            s3.put_object(Bucket=bucket, Key=completion_key,
                          Body=(json.dumps(completion, sort_keys=True) + "\n").encode(),
                          ContentType="application/json", IfNoneMatch="*")
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                raise
            if _read_json(s3, bucket, completion_key) != completion:
                raise ValueError("Concurrent release wrote different metadata") from error
    return completion


def download_from_s3(bucket: str, name: str, version: str, release_id: str,
                     serving_git_sha: str, destination: Path, s3=None) -> dict:
    prefix = release_prefix(name, version, release_id)
    s3 = s3 or boto3.client("s3")
    completion = _read_json(s3, bucket, f"{prefix}/complete.json")
    if completion is None:
        raise FileNotFoundError("Export Job has not published complete.json")
    if (completion.get("model_name"), completion.get("model_version"), completion.get("release_id"),
        completion.get("serving_git_sha")) != (name, version, release_id, serving_git_sha):
        raise ValueError("Release identity differs from request")
    files = completion.get("files")
    if not isinstance(files, dict) or "release.json" not in files:
        raise ValueError("Release inventory missing")
    if destination.exists():
        raise FileExistsError(destination)
    with tempfile.TemporaryDirectory(prefix="m32-download-", dir=destination.parent) as temp:
        root = Path(temp) / "release"
        root.mkdir()
        for relative, expected in files.items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("Unsafe release inventory")
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, f"{prefix}/bundle/{relative}", str(target))
            if file_hash(target) != expected:
                raise ValueError(f"S3 artifact checksum mismatch: {relative}")
        metadata, _ = validate_release(root)
        if any(metadata[key] != completion[other] for key, other in
               (("model_name", "model_name"), ("model_version", "model_version"),
                ("mlflow_run_id", "mlflow_run_id"), ("source_run_id", "source_run_id"),
                ("git_sha", "training_git_sha"), ("artifact_checksum", "artifact_checksum"))):
            raise ValueError("Completion metadata differs from release bundle")
        root.rename(destination)
    return completion


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["export", "download"])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--serving-git-sha", required=True)
    parser.add_argument("--tracking-uri", default="http://mlflow.mlops.svc.cluster.local:5000")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.mode == "export":
        result = export_to_s3(args.bucket, args.model_name, args.model_version, args.release_id,
                              args.serving_git_sha, args.tracking_uri)
    else:
        if args.output is None:
            parser.error("--output is required for download")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result = download_from_s3(args.bucket, args.model_name, args.model_version, args.release_id,
                                  args.serving_git_sha, args.output)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
