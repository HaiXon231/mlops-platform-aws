"""Unified demo/verification CLI: `python -m demo.runner <command> --preset P --scenario S`.

Default is dry-run. Nothing touches AWS/GitHub unless `run --execute` is given and confirmed,
or a read-only `preflight`/`verify`/`recover` is requested explicitly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from demo.runner import engines, report, spec, training
from demo.runner.context import EVIDENCE_ROOT, Context, new_evidence_dir, utc_now
from demo.runner.preflight import passed, run_preflight
from demo.runner.recovery import VERIFIED, run_recovery
from demo.runner.shell import LiveShell
from demo.runner.verdict import PASSED, Verdict

STATE_DIR = EVIDENCE_ROOT / "state"
LOCK = EVIDENCE_ROOT / ".lock"


# --------------------------------------------------------------------------- helpers


def _load(preset_name: str, scenario_id: str | None):
    scenarios = spec.load_scenarios()
    preset = spec.load_preset(preset_name)
    if scenario_id is None:
        return preset, scenarios, None, None
    scenario_id = scenario_id.upper()
    if scenario_id not in scenarios:
        raise spec.SpecError(f"Unknown scenario {scenario_id}; available {sorted(scenarios)}")
    scenario = scenarios[scenario_id]
    return preset, scenarios, scenario, spec.resolve_inputs(preset, scenario)


def _context(sh, preset, scenario, values, evidence_dir=None) -> Context:
    return Context(sh=sh, scenario=scenario, values=dict(values), preset=preset["preset"],
                   evidence_dir=evidence_dir or new_evidence_dir(preset["preset"], scenario.id))


def acquire_lock(owner: str, path: Path = LOCK) -> None:
    """Local guard against two scenarios in parallel; the remote guard is the no_active_* preflight."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = json.loads(path.read_text() or "{}")
        try:
            os.kill(int(data.get("pid", 0)), 0)
            raise RuntimeError(f"another demo scenario is running: {data}")
        except (ProcessLookupError, ValueError):
            path.unlink()  # stale lock from a dead process
        except PermissionError:
            raise RuntimeError(f"another demo scenario is running: {data}") from None
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as stream:
        json.dump({"pid": os.getpid(), "owner": owner, "since": utc_now()}, stream)


def release_lock(path: Path = LOCK) -> None:
    path.unlink(missing_ok=True)


def save_state(ctx: Context) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{ctx.preset}_{ctx.scenario.id}.json"
    path.write_text(json.dumps({"saved": utc_now(), "values": ctx.values, "snapshot": ctx.snapshot},
                               indent=2, sort_keys=True, default=str) + "\n")
    return path


def load_state(ctx: Context) -> None:
    path = STATE_DIR / f"{ctx.preset}_{ctx.scenario.id}.json"
    if not path.exists():
        raise RuntimeError(f"no pre-run snapshot {path}; run `preflight` before triggering from the UI")
    data = json.loads(path.read_text())
    ctx.snapshot.update(data["snapshot"])
    ctx.values.update({k: v for k, v in data["values"].items() if k in {"model_version", "model_version_source"}})
    print(f"Using pre-run snapshot saved {data['saved']} ({path})")


def _print_checks(title: str, checks) -> None:
    print(f"\n{title}")
    for check in checks:
        print(f"  [{check.status:4}] {check.id:24} {check.detail}")


def _confirm_factory(scenario_id: str, assume_yes: bool):
    def confirm(step) -> bool:
        if assume_yes:
            return True
        answer = input(f"\nExecute [{step.effect}] {step.display}\nType {scenario_id} to confirm: ")
        return answer.strip().upper() == scenario_id
    return confirm


def _finish(ctx, mode, started, checks, plan, log, obs, verdict: Verdict | None, recovery=None) -> Path:
    rec_status, rec_checks = recovery if recovery else (None, [])
    refs = {"workflow_run": (obs or {}).get("workflow", {}).get("url"), "pull_request": (obs or {}).get("pr"),
            "release_commit": (obs or {}).get("release_commit"), "revert_commit": (obs or {}).get("revert_commit"),
            "analysis_run": ((obs or {}).get("analysis") or {}).get("name"),
            "mlflow_run": ((obs or {}).get("result") or {}).get("mlflow_run_id")}
    payload = {"scenario": ctx.scenario.id, "title": ctx.scenario.title, "preset": ctx.preset, "mode": mode,
               "scope": report.scope_label(mode, ctx.live, verdict.status if verdict else None, rec_status),
               "started": started, "finished": utc_now(), "inputs": ctx.values,
               "preflight": [c.as_dict() for c in checks], "plan": [s.as_dict() for s in plan], "log": log,
               "snapshot": ctx.snapshot, "observations": obs, "verdict": verdict.as_dict() if verdict else None,
               "recovery": {"status": rec_status, "checks": [c.as_dict() for c in rec_checks]},
               "recovery_notes": ctx.scenario.recovery.get("notes", []), "references": refs}
    path = report.write(ctx.evidence_dir, payload)
    print(f"\nScope: {payload['scope']}")
    if verdict:
        print(f"Verdict: {verdict.status} — {verdict.outcome} (expected {verdict.expected})")
        for reason in verdict.reasons:
            print(f"  - {reason}")
    if rec_status:
        print(f"Recovery: {rec_status}")
    print(f"Report: {path}")
    return path


# --------------------------------------------------------------------------- commands


def cmd_list(args) -> int:
    scenarios = spec.load_scenarios()
    presets = [args.preset] if args.preset else spec.list_presets()
    for name in presets:
        preset = spec.load_preset(name)
        print(f"\n{name}: {preset.get('description', '').strip()}")
        for scenario_id, scenario in scenarios.items():
            marker = "" if scenario_id in preset["scenarios"] else "  (no inputs)"
            print(f"  {scenario_id:3} {scenario.title:40} engine={scenario.engine:9} ~{scenario.estimated_minutes} min{marker}")
        if preset.get("sequence"):
            print(f"  sequence: {' → '.join(preset['sequence'])}")
    return 0


def cmd_validate(args) -> int:
    errors = []
    scenarios = spec.load_scenarios()
    configs: dict[str, str] = {}
    for name in spec.list_presets():
        preset = spec.load_preset(name)
        errors += spec.validate_preset(preset, scenarios)
        errors += [f"{name}: sequence has unknown {s}" for s in preset.get("sequence", []) if s not in scenarios]
        for scenario_id, inputs in preset["scenarios"].items():
            for key in ("training_config", "candidate_config"):
                if key in inputs:
                    text = training.render_demo_config(inputs["model_overrides"])
                    if configs.setdefault(inputs[key], text) != text:
                        errors.append(f"{inputs[key]} used with different overrides in two presets")
    for path, text in configs.items():
        if training.in_auto_fingerprint(path):
            errors.append(f"{path} is inside the auto-training fingerprint; rename to configs/demo-*.yaml")
        file = spec.ROOT / path
        if not file.exists() or file.read_text() != text:
            errors.append(f"{path} is missing or stale; run `python -m demo.runner render-configs --write`")
        else:
            try:
                training.validate_identity(file)
            except ValueError as error:
                errors.append(f"{path}: {error}")
    for error in errors:
        print("ERROR:", error)
    print(f"{len(scenarios)} scenarios, {len(spec.list_presets())} presets, {len(configs)} demo configs: "
          + ("OK" if not errors else f"{len(errors)} error(s)"))
    return 1 if errors else 0


def cmd_render_configs(args) -> int:
    written = {}
    for name in spec.list_presets():
        preset = spec.load_preset(name)
        for inputs in preset["scenarios"].values():
            for key in ("training_config", "candidate_config"):
                if key in inputs:
                    written[inputs[key]] = training.render_demo_config(inputs["model_overrides"])
    stale = [path for path, text in written.items()
             if not (spec.ROOT / path).exists() or (spec.ROOT / path).read_text() != text]
    if args.write:
        for path in stale:
            (spec.ROOT / path).write_text(written[path])
            training.validate_identity(spec.ROOT / path)
            print("wrote", path)
    for path in written:
        print(f"{'stale' if path in stale and not args.write else 'ok':5} {path}")
    return 1 if stale and not args.write else 0


def cmd_apply_candidate(args) -> int:
    preset, _, scenario, values = _load(args.preset, "S2")
    target = Path(args.target)
    target.write_text(training.patch_model_block(target.read_text(), values["model_overrides"]))
    print(f"Applied S2 model overrides to {target}: {values['model_overrides_text']}")
    return 0


def cmd_show(args) -> int:
    preset, _, scenario, values = _load(args.preset, args.scenario)
    values = engines.run_values(values, stamp="<timestamp>")
    print(f"# {scenario.id} — {scenario.title}  [preset {preset['preset']}]\n\nMục tiêu: {scenario.goal.strip()}\n")
    print("Initial state:")
    for line in scenario.raw.get("initial_state", []):
        print("  -", spec.render_text(line, values))
    print("\nInput change:", spec.render_text(scenario.raw.get("input_change", ""), values))
    print("\nTrigger (CLI):")
    for step in engines.build_plan(scenario, values):
        print(f"  [{step.effect}] {step.display}")
    ui = engines.github_ui(scenario, values)
    print(f"\nTrigger (GitHub UI): {ui['workflow']} · branch {ui['branch']}")
    for key, value in ui["fields"].items():
        print(f"    {key} = {value}")
    print("\nLive observation:")
    for item in scenario.raw.get("observe", []):
        print(f"  - {item['ui']}: {item['what']}")
    print("\nAcceptance criteria:")
    for line in scenario.verify["criteria"]:
        print("  -", line)
    print("\nRecovery checks:", ", ".join(scenario.recovery.get("checks", [])))
    for note in scenario.recovery.get("notes", []):
        print("  -", note)
    if values.get("model_version") == "champion":
        print("\nNote: model_version=champion is resolved from the MLflow alias during preflight.")
    return 0


def cmd_preflight(args, sh=None) -> int:
    preset, _, scenario, values = _load(args.preset, args.scenario)
    ctx = _context(sh or LiveShell(), preset, scenario, values)
    try:
        checks = run_preflight(ctx)
        _print_checks(f"Preflight {scenario.id} ({preset['preset']})", checks)
        path = save_state(ctx)
        print(f"\nSnapshot saved: {path}")
        _finish(ctx, "preflight", utc_now(), checks, [], [], {}, None)
        return 0 if passed(checks) else 1
    finally:
        ctx.close()


def cmd_preview(args, sh=None) -> int:
    preset, _, scenario, values = _load(args.preset, args.scenario)
    if scenario.engine != "pipeline":
        raise spec.SpecError("preview applies to the training experiment scenario (S2)")
    ctx = _context(sh or LiveShell(), preset, scenario, values)
    started = utc_now()
    try:
        result = engines.local_preview(ctx, train=args.train, reference_mae=args.reference_mae)
        print(json.dumps(result, indent=2, sort_keys=True))
        verdict = Verdict("PREVIEW", "preview_" + result["decision"], "preview", [
            f"local validation MAE {result['candidate_validation_mae']:.6f} vs {result['reference_source']} "
            f"{result['reference_validation_mae']:.6f}; required ≤ {result['required_max_mae']:.6f}"], result, "local")
        ctx.snapshot["preview"] = result
        _finish(ctx, "preview", started, [], [], [], {"preview": result}, verdict)
        return 0
    finally:
        ctx.close()


def cmd_run(args, sh=None) -> int:
    preset, _, scenario, values = _load(args.preset, args.scenario)
    values = engines.run_values(values)
    if not args.execute:
        return _dry_run(preset, scenario, values)
    ctx = _context(sh or LiveShell(), preset, scenario, values)
    acquire_lock(f"{preset['preset']}:{scenario.id}")
    started, log, plan, obs, verdict = utc_now(), [], [], {}, None
    try:
        checks = run_preflight(ctx)
        _print_checks("Preflight", checks)
        if not passed(checks):
            print("\nPreflight FAILED — nothing was triggered.")
            _finish(ctx, "execute", started, checks, [], log, {}, Verdict("INCONCLUSIVE", "preflight_failed",
                    scenario.verify["expect"], ["preflight failed; scenario not triggered"]))
            return 2
        save_state(ctx)
        plan = engines.build_plan(scenario, ctx.values)
        confirm = _confirm_factory(scenario.id, args.yes)
        if scenario.engine == "pipeline":
            ctx.snapshot["preview"] = engines.local_preview(ctx, train=args.train_preview)
            print(f"\nLocal preview: {ctx.snapshot['preview']['decision']} "
                  f"(MAE {ctx.snapshot['preview']['candidate_validation_mae']:.6f}, "
                  f"required ≤ {ctx.snapshot['preview']['required_max_mae']:.6f})")
            if ctx.values["mode"] != "automatic":
                verdict = Verdict("PREVIEW", "preview_" + ctx.snapshot["preview"]["decision"], "preview",
                                  ["mode=preview: no cloud trigger"], ctx.snapshot["preview"], "local")
                _finish(ctx, "preview", started, checks, plan, log, {"preview": ctx.snapshot["preview"]}, verdict)
                return 0
            obs, verdict = engines.execute_pipeline(ctx, plan, log, confirm)
        else:
            print("\nPlan:")
            for step in plan:
                print(f"  [{step.effect}] {step.display}")
            if not confirm(engines.Step("all", "aws", ["(the plan above)"])):
                print("Cancelled; nothing executed.")
                return 1
            if scenario.engine == "load":
                obs, verdict = engines.execute_load(ctx, plan, log)
            else:
                obs, verdict = engines.execute_dispatch(ctx, plan, log)
        recovery = run_recovery(ctx)
        _print_checks("Recovery", recovery[1])
        _finish(ctx, "execute", started, checks, plan, log, obs, verdict, recovery)
        return 0 if verdict.status == PASSED and recovery[0] == VERIFIED else 3
    finally:
        release_lock()
        ctx.close()


def _dry_run(preset, scenario, values) -> int:
    plan = engines.build_plan(scenario, values)
    print(f"DRY-RUN {scenario.id} — {scenario.title} [preset {preset['preset']}] — nothing will be executed\n")
    print("Preflight checks (read-only):", ", ".join(scenario.preflight))
    print("\nTrigger:")
    for step in plan:
        print(f"  [{step.effect:10}] {step.display}")
    ui = engines.github_ui(scenario, values)
    print(f"\nGitHub UI alternative: {ui['workflow']} · branch {ui['branch']} · fields {ui['fields']}")
    print(f"Verify: expect `{scenario.verify['expect']}` within {scenario.verify['timeout_minutes']} min")
    print("Recovery checks:", ", ".join(scenario.recovery.get("checks", [])))
    if values.get("model_version") == "champion":
        print("Note: model_version=champion will be resolved from the MLflow alias at preflight.")
    print(f"\nExecute: python -m demo.runner run --preset {preset['preset']} --scenario {scenario.id} --execute")
    return 0


def cmd_verify(args, sh=None) -> int:
    preset, _, scenario, values = _load(args.preset, args.scenario)
    if scenario.engine == "load":
        raise spec.SpecError("S5 is verified while it runs (run --execute); there is no workflow run to verify")
    ctx = _context(sh or LiveShell(), preset, scenario, values)
    started = utc_now()
    try:
        load_state(ctx)
        obs, verdict = engines.observe_run(ctx, str(args.run_id))
        recovery = run_recovery(ctx)
        _print_checks("Recovery", recovery[1])
        _finish(ctx, "verify", started, [], [], [{"step": "verify", "run_id": args.run_id, "at": started}],
                obs, verdict, recovery)
        return 0 if verdict.status == PASSED and recovery[0] == VERIFIED else 3
    finally:
        ctx.close()


def cmd_recover(args, sh=None) -> int:
    preset, _, scenario, values = _load(args.preset, args.scenario)
    ctx = _context(sh or LiveShell(), preset, scenario, values)
    try:
        load_state(ctx)
        status, checks = run_recovery(ctx)
        if scenario.engine == "pipeline" and ctx.snapshot.get("training_fingerprint"):
            now = training.fingerprint_at(ctx.sh, "origin/main")
            same = now == ctx.snapshot["training_fingerprint"]
            print(f"\nTraining fingerprint origin/main {'==' if same else '!='} pre-demo fingerprint")
            if not same:
                print(restore_guidance(ctx))
        _print_checks(f"Recovery {scenario.id}", checks)
        print(f"\nRecovery: {status}")
        for note in scenario.recovery.get("notes", []):
            print("  -", note)
        return 0 if status == VERIFIED else 3
    finally:
        ctx.close()


def restore_guidance(ctx) -> str:
    repo, stamp = ctx.values["github_repo"], utc_now().replace(":", "").replace("-", "")
    wt = f"artifacts/demo/worktrees/restore-{stamp}"
    return "\n".join([
        "Restore configs/training.yaml (only after a REJECTED S2; an approved+promoted config is the new standard):",
        f"  gh variable set AUTO_MLOPS_ENABLED --repo {repo} --body false",
        f"  git worktree add --detach {wt} origin/main",
        f"  uv run --locked python -m demo.runner restore-config --preset {ctx.preset} --target {wt}/configs/training.yaml",
        f"  git -C {wt} switch -c demo/s2-restore-{stamp} && git -C {wt} add configs/training.yaml",
        f"  git -C {wt} commit -m 'demo(S2): restore standard training configuration'",
        f"  git -C {wt} push -u origin demo/s2-restore-{stamp} && gh pr create --repo {repo} --base main --head demo/s2-restore-{stamp} --fill",
        "  # merge the PR in GitHub UI (auto is off, so CI will not retrain), then re-run:",
        f"  uv run --locked python -m demo.runner recover --preset {ctx.preset} --scenario S2   # must print fingerprint ==",
        f"  gh variable set AUTO_MLOPS_BASELINE_SHA --repo {repo} --body <merged-restore-commit-sha>",
        f"  gh variable set AUTO_MLOPS_ENABLED --repo {repo} --body true",
        f"  git worktree remove {wt}"])


def cmd_restore_config(args) -> int:
    preset, _, scenario, values = _load(args.preset, "S2")
    ctx = _context(None, preset, scenario, values, evidence_dir=STATE_DIR)
    load_state(ctx)
    text = ctx.snapshot.get("training_config_text")
    if not text:
        raise RuntimeError("snapshot has no pre-demo training config (S2 preflight in automatic mode records it)")
    Path(args.target).write_text(text if text.endswith("\n") else text + "\n")
    print(f"Restored pre-demo configs/training.yaml into {args.target}")
    return 0


def cmd_sequence(args) -> int:
    preset = spec.load_preset(args.preset)
    scenarios = spec.load_scenarios()
    order = preset.get("sequence") or sorted(preset["scenarios"])
    total = sum(scenarios[s].estimated_minutes for s in order)
    print(f"Demo sequence for preset {preset['preset']} (~{total} min, strictly one at a time):\n")
    for index, scenario_id in enumerate(order, 1):
        scenario = scenarios[scenario_id]
        print(f"{index}. {scenario_id} {scenario.title} (~{scenario.estimated_minutes} min, lock {scenario.lock})")
        print(f"     preflight: python -m demo.runner preflight --preset {preset['preset']} --scenario {scenario_id}")
        print(f"     run:       python -m demo.runner run --preset {preset['preset']} --scenario {scenario_id} --execute")
        if scenario.engine in {"training", "release"}:
            print(f"     UI path:   trigger in GitHub UI, then python -m demo.runner verify --preset {preset['preset']} "
                  f"--scenario {scenario_id} --run-id <id>")
        print(f"     gate:      continue only if Recovery = VERIFIED")
    for note in preset.get("sequence_notes", []):
        print("\nNote:", note)
    return 0


CHAMPION_HISTORY = STATE_DIR / "champion-history.json"


def _champion_client(args):
    """MlflowClient on --tracking-uri, the local :5000 tunnel if healthy, or a temporary port-forward."""
    import contextlib
    from mlflow import MlflowClient
    from demo.runner import probes
    if args.tracking_uri:
        return contextlib.nullcontext(MlflowClient(tracking_uri=args.tracking_uri))
    @contextlib.contextmanager
    def tunnel():
        with probes.port_forward("mlops", "mlflow", 5000, "/health") as url:
            yield MlflowClient(tracking_uri=url)
    return tunnel()


def _show_version(label, info) -> None:
    if not info:
        print(f"  {label}: (none)")
        return
    mae = info.get("validation_mae_minutes")
    print(f"  {label}: v{info['version']} {info['status']}/{info['validation_status']} "
          f"MAE={mae:.6f} key={str(info['validation_dataset_key'])[:12]}" if mae else f"  {label}: v{info['version']}")


def cmd_champion(args, client=None) -> int:
    from demo.runner import champion
    preset = spec.load_preset(args.preset)
    model = preset["defaults"]["model_name"]
    history = Path(args.history) if args.history else CHAMPION_HISTORY
    with (_champion_client(args) if client is None else __import__("contextlib").nullcontext(client)) as mlflow:
        if args.restore:
            record = champion.restore_target(mlflow, model, history)
            target, expected = record["from"], record["to"]
            print(f"Restore: undo change at {record['at']} (v{record['from']} -> v{record['to']})")
        elif args.version:
            target = args.version
            expected = None
        else:
            _show_version("champion", champion.current(mlflow, model))
            for record in json.loads(history.read_text()) if history.exists() else []:
                print(f"  history: {record['at']} v{record['from']} -> v{record['to']} {record.get('reason', '')}"
                      f"{' (restored ' + record['restored'] + ')' if record.get('restored') else ''}")
            return 0
        result = champion.plan(mlflow, model, target)
        print(f"Champion alias plan for {model}:")
        _show_version("now   ", result["before"])
        _show_version("target", result["after"])
        for note in result["notes"]:
            print("  note:", note)
        for error in result["errors"]:
            print("  ERROR:", error)
        if result["errors"]:
            return 2
        if result["noop"]:
            return 0
        expected = expected if args.restore else (result["before"] or {}).get("version")
        if not args.execute:
            print(f"\nDRY-RUN: nothing changed. Add --execute to move champion v{expected} -> v{target}.")
            return 0
        if not args.yes and input(f"Type v{target} to move champion: ").strip() != f"v{target}":
            print("Cancelled; nothing changed.")
            return 1
        if args.restore:
            done = champion.restore(mlflow, model, history, utc_now())
        else:
            done = champion.apply(mlflow, model, target, expected, history, utc_now(), args.reason or "")
        print(f"Champion is now v{done['confirmed']['version']} (read back). History: {history}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m demo.runner", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    lst = sub.add_parser("list", help="scenarios and presets")
    lst.add_argument("--preset")
    sub.add_parser("validate", help="schema, presets, demo configs, identity and fingerprint checks (local)")
    rc = sub.add_parser("render-configs", help="generate configs/demo-*.yaml from presets")
    rc.add_argument("--write", action="store_true")
    for name, helptext in (("show", "runbook card"), ("preflight", "read-only live checks + snapshot"),
                           ("run", "dry-run (default) or --execute"), ("verify", "verify a UI-triggered run"),
                           ("recover", "post-run recovery checks"), ("preview", "local gate preview (S2)")):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument("--preset", required=True)
        cmd.add_argument("--scenario", required=True)
        if name == "run":
            cmd.add_argument("--execute", action="store_true", help="perform the trigger (asks for confirmation)")
            cmd.add_argument("--yes", action="store_true", help="skip confirmation prompts")
            cmd.add_argument("--train-preview", action="store_true", help="S2: retrain locally before preview")
        if name == "verify":
            cmd.add_argument("--run-id", required=True)
        if name == "preview":
            cmd.add_argument("--train", action="store_true", help="train the candidate locally first")
            cmd.add_argument("--reference-mae", type=float, help="offline reference instead of cloud champion")
    for name in ("apply-candidate", "restore-config"):
        cmd = sub.add_parser(name, help=f"S2 helper: {name}")
        cmd.add_argument("--preset", required=True)
        cmd.add_argument("--target", required=True)
    seq = sub.add_parser("sequence", help="ordered demo plan")
    seq.add_argument("--preset", required=True)
    ch = sub.add_parser("champion", help="show / move / restore the MLflow champion alias (dry-run by default)")
    ch.add_argument("--preset", default="demo", help="preset whose model_name is used")
    target = ch.add_mutually_exclusive_group()
    target.add_argument("--version", help="explicit approved version to point champion at")
    target.add_argument("--restore", action="store_true", help="undo the latest recorded change")
    ch.add_argument("--execute", action="store_true", help="really move the alias (asks for confirmation)")
    ch.add_argument("--yes", action="store_true")
    ch.add_argument("--reason", help="note stored in the history")
    ch.add_argument("--tracking-uri", help="use this MLflow URI instead of a temporary port-forward")
    ch.add_argument("--history", help=argparse.SUPPRESS)
    return parser


COMMANDS = {"list": cmd_list, "validate": cmd_validate, "render-configs": cmd_render_configs, "show": cmd_show,
            "preflight": cmd_preflight, "run": cmd_run, "verify": cmd_verify, "recover": cmd_recover,
            "preview": cmd_preview, "apply-candidate": cmd_apply_candidate, "restore-config": cmd_restore_config,
            "sequence": cmd_sequence, "champion": cmd_champion}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (spec.SpecError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
