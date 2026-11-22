"""Champion alias staging: safety checks, optimistic concurrency, history and restore (fake MLflow)."""
from types import SimpleNamespace

import pytest

from demo.runner import champion, cli

KEY, METRIC = "c48a6e66", "validation_mae_minutes:v1:mean_abs_error:cleaned_seeded_month_sample"


class FakeMlflow:
    def __init__(self, versions, alias="3"):
        self.versions, self.alias, self.sets = versions, alias, []

    def get_model_version(self, model, number):
        if number not in self.versions:
            raise RuntimeError("RESOURCE_DOES_NOT_EXIST")
        status, tags = self.versions[number]
        return SimpleNamespace(version=number, status=status, run_id="r" + number, tags=tags)

    def get_model_version_by_alias(self, model, alias):
        if self.alias is None:
            raise RuntimeError("alias champion not found")
        return SimpleNamespace(version=self.alias)

    def set_registered_model_alias(self, model, alias, version):
        self.sets.append(version)
        self.alias = version


def tags(mae, status="approved", key=KEY):
    return {"validation_status": status, "validation_mae_minutes": str(mae), "validation_dataset_key": key,
            "validation_metric_definition": METRIC}


@pytest.fixture
def mlflow():
    return FakeMlflow({"1": ("READY", tags(3.8372)), "2": ("READY", tags(3.6949)), "3": ("READY", tags(3.6445)),
                       "5": ("READY", tags(9.9, status="rejected")), "6": ("READY", tags(3.5, key="other")),
                       "7": ("PENDING_REGISTRATION", tags(3.5))})


def test_plan_to_v2_explains_new_gate_threshold(mlflow):
    result = champion.plan(mlflow, "m", "2")
    assert result["errors"] == [] and not result["noop"]
    assert "needs MAE <= 3.657951" in result["notes"][0]       # 3.6949 * 0.99
    assert mlflow.sets == [], "plan is read-only"


@pytest.mark.parametrize("target,error", [("4", "does not exist"), ("5", "must be READY/approved"),
                                          ("7", "must be READY/approved"), ("6", "different validation dataset")])
def test_unsafe_targets_are_refused(mlflow, tmp_path, target, error):
    assert any(error in e for e in champion.plan(mlflow, "m", target)["errors"])
    with pytest.raises(ValueError, match=error):
        champion.apply(mlflow, "m", target, "3", tmp_path / "h.json", "t1")
    assert mlflow.alias == "3"


def test_apply_reads_back_and_records_history_then_restore(mlflow, tmp_path):
    history = tmp_path / "h.json"
    done = champion.apply(mlflow, "m", "2", "3", history, "t1", "demo v4")
    assert done["confirmed"]["version"] == "2" and mlflow.alias == "2"
    champion.restore(mlflow, "m", history, "t2")
    assert mlflow.alias == "3" and mlflow.sets == ["2", "3"]
    with pytest.raises(ValueError, match="no recorded"):
        champion.restore_target(mlflow, "m", history)          # already restored


def test_apply_refuses_when_alias_moved_since_plan(mlflow, tmp_path):
    mlflow.alias = "1"
    with pytest.raises(RuntimeError, match="since the plan"):
        champion.apply(mlflow, "m", "2", "3", tmp_path / "h.json", "t1")
    assert mlflow.sets == []


def test_restore_refuses_after_a_release_promoted_a_new_champion(mlflow, tmp_path):
    history = tmp_path / "h.json"
    champion.apply(mlflow, "m", "2", "3", history, "t1")
    mlflow.versions["4"] = ("READY", tags(3.6139))
    mlflow.alias = "4"                                         # release promoted v4
    with pytest.raises(RuntimeError, match="refusing to undo"):
        champion.restore_target(mlflow, "m", history)


def test_noop_when_already_champion(mlflow, tmp_path):
    assert champion.apply(mlflow, "m", "3", "3", tmp_path / "h.json", "t")["noop"]
    assert mlflow.sets == []


def test_port_forward_reuses_a_healthy_standard_tunnel(monkeypatch):
    from demo.runner import probes
    monkeypatch.setattr(probes, "_healthy", lambda url: url == "http://127.0.0.1:5000/health")
    monkeypatch.setattr(probes.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn kubectl"))
    with probes.port_forward("mlops", "mlflow", 5000, "/health") as url:
        assert url == "http://127.0.0.1:5000"


def test_cli_is_dry_run_by_default_and_executes_with_confirmation(mlflow, tmp_path, capsys):
    history = str(tmp_path / "h.json")
    args = cli.build_parser().parse_args(["champion", "--version", "2", "--history", history])
    assert cli.cmd_champion(args, client=mlflow) == 0 and mlflow.sets == []
    assert "DRY-RUN" in capsys.readouterr().out
    args = cli.build_parser().parse_args(["champion", "--version", "2", "--history", history, "--execute", "--yes"])
    assert cli.cmd_champion(args, client=mlflow) == 0 and mlflow.alias == "2"
    args = cli.build_parser().parse_args(["champion", "--version", "5", "--history", history])
    assert cli.cmd_champion(args, client=mlflow) == 2
    args = cli.build_parser().parse_args(["champion", "--restore", "--execute", "--yes", "--history", history])
    assert cli.cmd_champion(args, client=mlflow) == 0 and mlflow.alias == "3"
