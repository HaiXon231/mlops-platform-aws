"""Release flow with a canary: outcome detection, rollback to Git and champion safety."""

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = load_script("m32_verify")
IMAGE = "160616607747.dkr.ecr.ap-southeast-1.amazonaws.com/mlops-aws-inference-dev@sha256:" + "a" * 64
EXPECTED = {"image": IMAGE, "release_id": "m32-9-1", "artifact_checksum": "d" * 64, "model_version": "2"}


def deployment(image=IMAGE, generation=7):
    return {"metadata": {"generation": generation},
            "spec": {"template": {
                "metadata": {"annotations": {"mlops.aws/release-id": "m32-9-1",
                                             "mlops.aws/artifact-checksum": "d" * 64},
                             "labels": {"mlops.aws/model-version": "2"}},
                "spec": {"containers": [{"image": image}]}}}}


def rollout(**status):
    base = {"workloadObservedGeneration": "7", "currentPodHash": "new", "stableRS": "old", "phase": "Progressing"}
    return {"status": base | status}


def test_progressing_until_controller_has_seen_this_template_generation():
    assert verify.rollout_state(deployment(), rollout(workloadObservedGeneration="6"), EXPECTED) == "progressing"
    assert verify.rollout_state(deployment(image=IMAGE.replace("a", "b")), rollout(), EXPECTED) == "progressing"
    assert verify.rollout_state(deployment(), rollout(), EXPECTED) == "progressing"


def test_promoted_only_when_new_replicaset_is_stable_and_healthy():
    assert verify.rollout_state(deployment(), rollout(phase="Healthy", stableRS="new"), EXPECTED) == "promoted"
    # Healthy on the previous release must not count as this release being live.
    assert verify.rollout_state(deployment(), rollout(phase="Healthy", stableRS="old"), EXPECTED) == "progressing"


def test_aborted_when_the_new_replicaset_is_rejected():
    assert verify.rollout_state(deployment(), rollout(phase="Degraded", abort=True), EXPECTED) == "aborted"
    assert verify.rollout_state(deployment(), rollout(abort=True), EXPECTED) == "aborted"
    # Seen on the cluster: a canary that never became Ready is rejected by ProgressDeadlineExceeded,
    # which leaves status.abort unset and only sets phase=Degraded.
    assert verify.rollout_state(deployment(), rollout(phase="Degraded"), EXPECTED) == "aborted"
    # A Degraded rollout whose current ReplicaSet is already stable is not a rejected canary.
    assert verify.rollout_state(deployment(), rollout(phase="Degraded", currentPodHash="old"), EXPECTED) == "progressing"


def test_live_state_requires_ready_pods_with_the_released_digest(monkeypatch):
    pod = {"metadata": {}, "spec": {"containers": [{"image": IMAGE}]},
           "status": {"containerStatuses": [{"ready": True, "imageID": "docker://x@sha256:" + "a" * 64}]}}
    answers = {"deployment": deployment(),
               "rollout.argoproj.io": rollout(phase="Healthy", stableRS="new"),
               "pods": {"items": [pod]}}
    monkeypatch.setattr(verify, "command_json", lambda *args: answers[args[3]])
    assert verify.live_state(EXPECTED) == "promoted"
    pod["status"]["containerStatuses"][0]["ready"] = False
    assert verify.live_state(EXPECTED) == "progressing"
    pod["status"]["containerStatuses"][0] = {"ready": True, "imageID": "docker://x@sha256:" + "b" * 64}
    assert verify.live_state(EXPECTED) == "progressing"


def test_wait_for_outcome_returns_abort_without_waiting_for_argo_health(monkeypatch):
    monkeypatch.setattr(verify, "current_main", lambda: "commit")
    monkeypatch.setattr(verify, "live_state", lambda expected: "aborted")
    monkeypatch.setattr(verify, "application_matches", lambda commit: pytest.fail("Argo is Degraded on abort"))
    assert verify.wait_for_outcome(EXPECTED, "commit") == "aborted"


def test_wait_for_outcome_refuses_when_main_moves(monkeypatch):
    monkeypatch.setattr(verify, "current_main", lambda: "other")
    with pytest.raises(RuntimeError, match="main moved"):
        verify.wait_for_outcome(EXPECTED, "commit")


def test_cli_exits_with_code_three_for_an_aborted_canary(monkeypatch, tmp_path, capsys):
    expected = tmp_path / "expected.json"
    expected.write_text('{"image": "i", "release_id": "r"}')
    monkeypatch.setattr(verify, "wait_for_outcome", lambda release, commit: "aborted")
    monkeypatch.setattr("sys.argv", ["m32_verify.py", "--expected", str(expected), "--commit", "c",
                                      "--mlflow-uri", "http://x", "--inference-uri", "http://y", "--wait-outcome"])
    with pytest.raises(SystemExit) as stop:
        verify.main()
    assert stop.value.code == verify.EXIT_CANARY_ABORTED == 3
    assert '"canary": "aborted"' in capsys.readouterr().out


def test_rollback_commit_is_made_only_after_outcome_and_never_promotes_champion():
    script = (ROOT / "scripts/m32_release.sh").read_text()
    assert script.count("git commit") == 1 and script.count("git revert --no-edit") == 1
    outcome = script.index("--wait-outcome")
    revert = script.index("git revert --no-edit")
    promote_run = script.index("m32_verify.py", script.index("[[ \"$outcome\" -eq 0 ]]"))
    assert script.index("git commit") < outcome < revert < promote_run
    rollback = script[script.index('if [[ "$outcome" -eq 3 ]]'):script.index('[[ "$outcome" -eq 0 ]]')]
    assert "champion alias was NOT changed" in rollback
    assert "--wait-reverted" in rollback and "exit 1" in rollback
    assert 'rev-parse origin/main)" != "$commit"' in rollback, "refuse to revert if main moved"
    assert "set_registered_model_alias" not in script


def test_runner_rbac_can_read_rollout_and_analysis_but_not_change_them():
    docs = list(yaml.safe_load_all((ROOT / "deploy/bootstrap/m32-github-release-rbac.yaml").read_text()))
    role = next(doc for doc in docs if doc["kind"] == "Role" and doc["metadata"]["namespace"] == "mlops")
    argo = [rule for rule in role["rules"] if rule["apiGroups"] == ["argoproj.io"]]
    assert {tuple(rule["resources"]) for rule in argo} == {("rollouts",), ("analysisruns",)}
    assert all(set(rule["verbs"]) <= {"get", "list"} for rule in argo)
    rollouts = next(rule for rule in argo if rule["resources"] == ["rollouts"])
    assert rollouts["resourceNames"] == ["inference-api"]


def test_workflow_still_restores_cidrs_even_when_the_canary_fails():
    workflow = (ROOT / ".github/workflows/m32-release.yml").read_text()
    assert re.search(r"close --state-file[^\n]*", workflow)
    assert "if: always()" in workflow
