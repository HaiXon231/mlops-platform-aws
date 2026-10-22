import importlib.util
import io
import json
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[2] / "deploy/backup/backup.py"
SPEC = importlib.util.spec_from_file_location("cloud_backup", PATH)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class FakeS3:
    def __init__(self, content, info):
        self.content = content
        self.info = info

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self.info).encode())}

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.content)


def test_restore_rejects_bad_checksum_before_touching_postgres(tmp_path, monkeypatch):
    dump = tmp_path / "backup.dump"
    info = {"dump_key": "postgres/run.dump", "database": "mlflow", "sha256": "wrong", "size_bytes": 4}
    monkeypatch.setenv("PGDATABASE", "mlflow")
    with pytest.raises(ValueError, match="checksum"):
        backup.fetch(FakeS3(b"data", info), "backups", "postgres/run.dump", dump)


def test_primary_restore_refuses_nonempty_database(tmp_path, monkeypatch):
    monkeypatch.setenv("PGDATABASE", "mlflow")
    monkeypatch.setattr(backup, "pg", lambda *args, **kwargs: "34")
    with pytest.raises(ValueError, match="not empty"):
        backup.restore_primary(tmp_path / "unused.dump", {})


def test_backup_key_cannot_escape_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("PGDATABASE", "mlflow")
    with pytest.raises(ValueError, match="postgres"):
        backup.fetch(FakeS3(b"", {}), "backups", "../other.dump", tmp_path / "unused.dump")
