"""Release identity, bounded EKS access and promotion gates."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gitops = load_script("m32_gitops")
network = load_script("m32_eks_cidr")
verify = load_script("m32_verify")
IMAGE = gitops.ECR + "@sha256:" + "a" * 64


def release_metadata():
    return {"model_name": "nyc-taxi-trip-duration", "model_version": "1",
            "release_id": "m32-123-1", "serving_git_sha": "c" * 40,
            "artifact_checksum": "d" * 64, "mlflow_run_id": "mlflow-run",
            "source_run_id": "training-run", "git_sha": "e" * 40}


def test_prepare_and_single_manifest_update(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    metadata = release_metadata()
    metadata.pop("release_id")
    metadata.pop("serving_git_sha")
    (bundle / "release.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(gitops, "validate_release", lambda path: (json.loads((path / "release.json").read_text()), None))
    prepared = gitops.prepare_bundle(bundle, "m32-123-1", "c" * 40)
    assert prepared["release_id"] == "m32-123-1"
    assert prepared["serving_git_sha"] == "c" * 40
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text(gitops.DEPLOYMENT.read_text())
    result = gitops.update_manifest(bundle, IMAGE, deployment)
    rendered = yaml.safe_load(deployment.read_text())
    assert result["image"] == IMAGE
    assert rendered["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE
    assert rendered["spec"]["template"]["metadata"]["annotations"]["mlops.aws/release-id"] == "m32-123-1"
    with pytest.raises(ValueError, match="digest"):
        gitops.update_manifest(bundle, gitops.ECR + ":mutable", deployment)
    with pytest.raises(ValueError, match="another release"):
        gitops.prepare_bundle(bundle, "m32-124-1", "c" * 40)


def test_same_digest_new_release_id_still_updates_manifest(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    metadata = release_metadata()
    (bundle / "release.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(gitops, "validate_release", lambda path: (json.loads((path / "release.json").read_text()), None))
    deployment = tmp_path / "deployment.yaml"
    deployment.write_text(gitops.DEPLOYMENT.read_text())
    doc = yaml.safe_load(deployment.read_text())
    doc["spec"]["template"]["spec"]["containers"][0]["image"] = IMAGE
    deployment.write_text(yaml.safe_dump(doc))
    before = deployment.read_text()
    gitops.update_manifest(bundle, IMAGE, deployment)
    assert deployment.read_text() != before


def test_cidr_open_and_exact_restore(monkeypatch, tmp_path):
    current = {"status": "ACTIVE", "public": True, "private": True,
               "cidrs": ["14.187.95.89/32"]}
    calls = []
    monkeypatch.setattr(network, "wait_active", lambda: current.copy())
    monkeypatch.setattr(network, "wait_no_updates", lambda: None)
    monkeypatch.setattr(network, "cluster_state", lambda: current.copy())
    monkeypatch.setattr(network, "runner_cidr", lambda: "203.0.113.9/32")

    def update(cidrs):
        calls.append(cidrs.copy())
        current["cidrs"] = cidrs.copy()

    monkeypatch.setattr(network, "update_cidrs", update)
    state = tmp_path / "cidrs.json"
    network.open_access(state)
    assert current["cidrs"] == ["14.187.95.89/32", "203.0.113.9/32"]
    assert json.loads(state.read_text())["original_cidrs"] == ["14.187.95.89/32"]
    network.close_access(state)
    assert current["cidrs"] == ["14.187.95.89/32"]
    assert len(calls) == 2


def test_cidr_refuses_concurrent_change(monkeypatch, tmp_path):
    state = tmp_path / "cidrs.json"
    state.write_text(json.dumps({"cluster": network.CLUSTER,
                                 "original_cidrs": ["14.187.95.89/32"],
                                 "temporary_cidrs": ["14.187.95.89/32", "203.0.113.9/32"]}))
    monkeypatch.setattr(network, "wait_active", lambda: {"status": "ACTIVE", "public": True,
                                                         "private": True, "cidrs": ["198.51.100.1/32"]})
    monkeypatch.setattr(network, "wait_no_updates", lambda: None)
    monkeypatch.setattr(network, "update_cidrs", lambda cidrs: pytest.fail("Must not overwrite admin change"))
    with pytest.raises(RuntimeError, match="concurrently"):
        network.close_access(state)


def test_promotion_requires_current_gitops_revision(monkeypatch):
    expected = release_metadata() | {"image": IMAGE}
    monkeypatch.setattr(verify, "current_main", lambda: "old-revision")
    monkeypatch.setattr(verify, "application_matches", lambda commit: True)
    monkeypatch.setattr(verify, "workload_matches", lambda release: True)
    with pytest.raises(RuntimeError, match="no longer"):
        verify.promote(object(), expected, "new-revision")


def test_idempotent_promotion_and_readback(monkeypatch):
    expected = release_metadata() | {"image": IMAGE}
    monkeypatch.setattr(verify, "current_main", lambda: "commit")
    monkeypatch.setattr(verify, "application_matches", lambda commit: True)
    monkeypatch.setattr(verify, "workload_matches", lambda release: True)
    monkeypatch.setattr(verify, "verify_registry", lambda client, release: None)

    class Client:
        calls = 0
        version = "1"

        def get_model_version_by_alias(self, name, alias):
            return type("Alias", (), {"version": self.version, "run_id": "mlflow-run"})()

        def set_registered_model_alias(self, name, alias, version):
            self.calls += 1
            self.version = version

    client = Client()
    assert verify.promote(client, expected, "commit") == "already-promoted"
    assert client.calls == 0
    client.version = "2"
    assert verify.promote(client, expected, "commit") == "promoted"
    assert client.calls == 1


def test_workflow_has_one_release_commit_and_no_legacy_handshake():
    workflow = (ROOT / ".github/workflows/m32-release.yml").read_text()
    iam = (ROOT / "infra/terraform/platform/m32_release_iam.tf").read_text()
    access = (ROOT / "infra/terraform/platform/eks.tf").read_text()
    assert workflow.count("git commit") == 0  # One commit is made inside m32_release.sh.
    assert (ROOT / "scripts/m32_release.sh").read_text().count("git commit") == 1
    assert "inference.release_s3" not in workflow
    assert "PostSync" not in workflow
    assert "scripts/m32_eks_cidr.py close" in workflow and "if: always()" in workflow
    assert "repo:HaiXon231@204581707/mlops-platform-aws@1424395869:ref:refs/heads/main" in iam
    assert 'kubernetes_groups = ["mlops-github-release"]' in access
