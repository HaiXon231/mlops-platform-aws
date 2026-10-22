import io
import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError

from inference import release_s3
from inference import promote
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/m32_gitops.py"
_SPEC = importlib.util.spec_from_file_location("m32_gitops", _SCRIPT)
m32_gitops = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m32_gitops)

IMAGE = m32_gitops.ECR + "@sha256:" + "a" * 64
CONTROL = m32_gitops.ECR + "@sha256:" + "b" * 64
BUCKET = "mlops-aws-model-artifacts-dev-160616607747-ap-southeast-1"
SHA = "c" * 40


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Bucket, Key])}

    def upload_file(self, Filename, Bucket, Key):
        self.objects[Bucket, Key] = Path(Filename).read_bytes()

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self.objects[Bucket, Key])

    def put_object(self, Bucket, Key, Body, **kwargs):
        if kwargs.get("IfNoneMatch") == "*" and (Bucket, Key) in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "exists"}}, "PutObject")
        self.objects[Bucket, Key] = Body


def test_release_identity_and_duplicate_export(monkeypatch, tmp_path):
    s3 = FakeS3()
    metadata = {"mlflow_run_id": "mlflow-run", "source_run_id": "training-run", "git_sha": SHA,
                "artifact_checksum": "d" * 64}

    def fake_export(uri, name, version, output):
        output.mkdir()
        (output / "release.json").write_text(json.dumps(metadata))
        (output / "model.txt").write_text("model")
        return metadata

    monkeypatch.setattr(release_s3, "export_model", fake_export)
    first = release_s3.export_to_s3(BUCKET, "nyc-taxi-trip-duration", "1", "m32-10", SHA,
                                    "http://mlflow", s3)
    assert first["training_git_sha"] == SHA
    assert first["serving_git_sha"] == SHA
    assert release_s3.export_to_s3(BUCKET, "nyc-taxi-trip-duration", "1", "m32-10", SHA,
                                   "http://mlflow", s3) == first
    with pytest.raises(ValueError, match="another source"):
        release_s3.export_to_s3(BUCKET, "nyc-taxi-trip-duration", "1", "m32-10", "e" * 40,
                                "http://mlflow", s3)
    with pytest.raises(ValueError, match="positive"):
        release_s3.release_prefix("nyc-taxi-trip-duration", "latest", "m32-11")


def test_download_rejects_corrupt_bundle(monkeypatch, tmp_path):
    s3 = FakeS3()
    key = release_s3.release_prefix("nyc-taxi-trip-duration", "1", "m32-10")
    completion = {"model_name": "nyc-taxi-trip-duration", "model_version": "1", "release_id": "m32-10",
                  "serving_git_sha": SHA, "files": {"release.json": "0" * 64}}
    s3.objects[BUCKET, key + "/complete.json"] = json.dumps(completion).encode()
    s3.objects[BUCKET, key + "/bundle/release.json"] = b"corrupt"
    with pytest.raises(ValueError, match="checksum"):
        release_s3.download_from_s3(BUCKET, "nyc-taxi-trip-duration", "1", "m32-10", SHA,
                                    tmp_path / "release", s3)
    assert not (tmp_path / "release").exists()


def test_gitops_stage_finalize_and_duplicate(monkeypatch, tmp_path):
    export = tmp_path / "export"
    inference = tmp_path / "inference"
    export.mkdir()
    inference.mkdir()
    (export / "kustomization.yaml").write_text("resources:\n  - serviceaccount.yaml\n")
    (inference / "kustomization.yaml").write_text("resources:\n  - deployment.yaml\n")
    shutil.copy(m32_gitops.INFERENCE / "deployment.yaml", inference / "deployment.yaml")
    monkeypatch.setattr(m32_gitops, "EXPORT", export)
    monkeypatch.setattr(m32_gitops, "INFERENCE", inference)
    first = m32_gitops.stage("nyc-taxi-trip-duration", "1", "m32-10", SHA, CONTROL, BUCKET)
    assert m32_gitops.stage("nyc-taxi-trip-duration", "1", "m32-10", SHA, CONTROL, BUCKET) == first
    with pytest.raises(ValueError, match="different inputs"):
        m32_gitops.stage("nyc-taxi-trip-duration", "1", "m32-10", "d" * 40, CONTROL, BUCKET)
    completion = {"model_name": "nyc-taxi-trip-duration", "model_version": "1", "release_id": "m32-10",
                  "serving_git_sha": SHA, "training_git_sha": "e" * 40, "artifact_checksum": "f" * 64,
                  "mlflow_run_id": "mlflow-run", "source_run_id": "training-run"}
    result = m32_gitops.finalize(completion, IMAGE, CONTROL, BUCKET)
    assert result["image"] == IMAGE
    deployed = yaml.safe_load((inference / "deployment.yaml").read_text())
    assert deployed["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE
    assert deployed["spec"]["template"]["metadata"]["annotations"]["mlops.aws/release-id"] == "m32-10"
    hook = yaml.safe_load((inference / "post-sync-verification.yaml").read_text())
    assert hook["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PostSync"
    assert "post-sync-verification.yaml" in yaml.safe_load((inference / "kustomization.yaml").read_text())["resources"]
    with pytest.raises(ValueError, match="differ"):
        m32_gitops.finalize({**completion, "serving_git_sha": "d" * 40}, IMAGE, CONTROL, BUCKET)


def test_promotion_requires_all_verification_and_is_idempotent(monkeypatch):
    checked = []
    monkeypatch.setattr(promote, "acquire_lease", lambda kube, release: checked.append("lock"))
    monkeypatch.setattr(promote, "release_lease", lambda kube, release: checked.append("unlock"))
    monkeypatch.setattr(promote, "verify_workload", lambda kube, expected: checked.append("workload"))
    monkeypatch.setattr(promote, "verify_api", lambda expected: checked.append("api"))
    monkeypatch.setattr(promote, "verify_registry", lambda client, expected: checked.append("registry"))

    class Client:
        def __init__(self):
            self.version = "1"
            self.set_calls = []

        def get_model_version_by_alias(self, name, alias):
            return type("Version", (), {"version": self.version})()

        def set_registered_model_alias(self, name, alias, version):
            self.set_calls.append((name, alias, version))

    client = Client()
    expected = {"release_id": "m32-10", "model_name": "nyc-taxi-trip-duration", "model_version": "1"}
    assert promote.promote(object(), client, expected) == "already-promoted"
    assert not client.set_calls
    assert checked == ["lock", "workload", "api", "registry", "workload", "unlock"]
    checked.clear()
    client.version = "2"
    monkeypatch.setattr(promote, "verify_api", lambda expected: (_ for _ in ()).throw(ValueError("unready")))
    with pytest.raises(ValueError, match="unready"):
        promote.promote(object(), client, expected)
    assert not client.set_calls
    assert checked[-1] == "unlock"


def test_workload_verifier_rejects_wrong_running_digest():
    expected = {"image": IMAGE, "model_version": "1", "release_id": "m32-10"}
    deployment = {"metadata": {"generation": 2},
                  "spec": {"replicas": 1, "template": {"metadata": {
                      "labels": {"mlops.aws/model-version": "1"},
                      "annotations": {"mlops.aws/release-id": "m32-10"}},
                      "spec": {"containers": [{"image": IMAGE}]}}},
                  "status": {"observedGeneration": 2, "readyReplicas": 1}}
    pod = {"metadata": {}, "spec": {"containers": [{"image": IMAGE}]},
           "status": {"containerStatuses": [{"ready": True, "imageID": IMAGE}]}}

    class Kube:
        def call(self, method, path):
            return deployment if "deployments/" in path else {"items": [pod]}

    promote.verify_workload(Kube(), expected)
    pod["status"]["containerStatuses"][0]["imageID"] = CONTROL
    with pytest.raises(ValueError, match="digest"):
        promote.verify_workload(Kube(), expected)


def test_oidc_and_workflow_scope():
    root = Path(__file__).resolve().parents[2]
    iam = (root / "infra/terraform/platform/m32_release_iam.tf").read_text()
    workflow = (root / ".github/workflows/m32-release.yml").read_text()
    assert "repo:HaiXon231@204581707/mlops-platform-aws@1424395869:ref:refs/heads/main" in iam
    assert 'data "aws_iam_openid_connect_provider" "github_actions"' in iam
    assert 'resource "aws_iam_openid_connect_provider"' not in iam
    assert '"s3:GetObject"' in iam and '"ecr:PutImage"' in iam
    assert "GITHUB_REPOSITORY_ID\" = 1424395869" in workflow
    assert "id-token: write" in workflow
    assert "workflow_dispatch:" in workflow
