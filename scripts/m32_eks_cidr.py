"""Temporarily admit one GitHub runner IPv4 to the existing EKS public API."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CLUSTER = "mlops-aws-dev"
TIMEOUT_SECONDS = 1200


def aws(*arguments: str) -> dict:
    result = subprocess.run(["aws", *arguments, "--output", "json"], check=True,
                            capture_output=True, text=True)
    return json.loads(result.stdout)


def cluster_state() -> dict:
    cluster = aws("eks", "describe-cluster", "--name", CLUSTER)["cluster"]
    config = cluster["resourcesVpcConfig"]
    return {"status": cluster["status"], "public": config["endpointPublicAccess"],
            "private": config["endpointPrivateAccess"], "cidrs": config["publicAccessCidrs"]}


def runner_cidr() -> str:
    result = subprocess.run(["curl", "-4", "--fail", "--silent", "--show-error", "--max-time", "15",
                             "https://checkip.amazonaws.com"], check=True, capture_output=True, text=True)
    address = result.stdout.strip()
    parsed = ipaddress.ip_address(address)
    if parsed.version != 4 or not parsed.is_global:
        raise ValueError("Runner egress address is not a public IPv4 address")
    return f"{parsed}/32"


def same_cidrs(first: list[str], second: list[str]) -> bool:
    return len(first) == len(second) and set(first) == set(second)


def wait_active() -> dict:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        state = cluster_state()
        if state["status"] == "ACTIVE":
            return state
        time.sleep(15)
    raise TimeoutError("EKS cluster did not return to ACTIVE")


def wait_no_updates() -> None:
    """Observe accepted updates, including one interrupted before its ID was saved."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        ids = aws("eks", "list-updates", "--name", CLUSTER).get("updateIds", [])
        states = [aws("eks", "describe-update", "--name", CLUSTER,
                      "--update-id", update_id)["update"]["status"] for update_id in ids]
        if not any(status in {"InProgress", "Pending"} for status in states):
            return
        time.sleep(15)
    raise TimeoutError("An EKS cluster update is still in progress")


def update_cidrs(cidrs: list[str]) -> None:
    config = {"endpointPublicAccess": True, "endpointPrivateAccess": True,
              "publicAccessCidrs": cidrs}
    result = aws("eks", "update-cluster-config", "--name", CLUSTER,
                 "--resources-vpc-config", json.dumps(config))
    update_id = result["update"]["id"]
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        update = aws("eks", "describe-update", "--name", CLUSTER,
                     "--update-id", update_id)["update"]
        if update["status"] == "Successful":
            break
        if update["status"] in {"Failed", "Cancelled"}:
            raise RuntimeError(f"EKS endpoint update {update_id}: {update['status']} {update.get('errors')}")
        time.sleep(15)
    else:
        raise TimeoutError(f"EKS endpoint update {update_id} timed out")
    final = wait_active()
    if not same_cidrs(final["cidrs"], cidrs) or not final["public"] or not final["private"]:
        raise RuntimeError(f"EKS endpoint postcondition failed: {final}")


def save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def summary(message: str) -> None:
    print(message, flush=True)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(message + "\n")


def open_access(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"CIDR recovery state already exists: {path}")
    wait_no_updates()
    before = wait_active()
    if not before["public"] or not before["private"] or not before["cidrs"]:
        raise ValueError("Expected both EKS endpoints enabled and a restricted public CIDR list")
    if "0.0.0.0/0" in before["cidrs"] or "::/0" in before["cidrs"]:
        raise ValueError("Refusing unrestricted EKS public API access")
    runner = runner_cidr()
    temporary = before["cidrs"] if runner in before["cidrs"] else [*before["cidrs"], runner]
    state = {"cluster": CLUSTER, "original_cidrs": before["cidrs"],
             "temporary_cidrs": temporary, "runner_cidr": runner}
    # Persist recovery data before the AWS mutation, including interrupted updates.
    save_state(path, state)
    summary(f"EKS original CIDRs: `{json.dumps(before['cidrs'])}`; runner CIDR: `{runner}`")
    if temporary != before["cidrs"]:
        update_cidrs(temporary)
    summary(f"EKS temporary CIDRs verified: `{json.dumps(temporary)}`")


def close_access(path: Path) -> None:
    if not path.exists():
        summary("No EKS CIDR change was started.")
        return
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved["cluster"] != CLUSTER:
        raise ValueError("CIDR recovery state belongs to another cluster")
    wait_no_updates()
    current = wait_active()
    if not current["public"] or not current["private"]:
        raise RuntimeError("EKS endpoint mode changed during release; manual recovery required")
    original = saved["original_cidrs"]
    temporary = saved["temporary_cidrs"]
    if same_cidrs(current["cidrs"], temporary) and not same_cidrs(temporary, original):
        update_cidrs(original)
    elif not same_cidrs(current["cidrs"], original):
        raise RuntimeError(f"EKS CIDRs changed concurrently: {current['cidrs']}; manual recovery required")
    final = cluster_state()
    if not same_cidrs(final["cidrs"], original) or not final["public"] or not final["private"]:
        raise RuntimeError(f"EKS original endpoint configuration was not restored: {final}")
    summary(f"EKS CIDRs restored and verified: `{json.dumps(original)}`")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["open", "close"])
    parser.add_argument("--state-file", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.mode == "open":
            open_access(arguments.state_file)
        else:
            close_access(arguments.state_file)
    except Exception as error:
        print(f"EKS CIDR {arguments.mode} failed: {error}", file=sys.stderr)
        if arguments.state_file.exists():
            saved = json.loads(arguments.state_file.read_text(encoding="utf-8"))
            summary(f"Manual recovery: restore exact EKS publicAccessCidrs `{json.dumps(saved['original_cidrs'])}` "
                    f"for `{CLUSTER}`; keep both endpoint access modes enabled, then verify with DescribeCluster.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
