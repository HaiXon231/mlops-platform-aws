"""Turn a Prometheus instant-vector response into stable/canary request shares."""

from __future__ import annotations

import argparse
import json
import sys


def shares(payload: dict, stable: str, current: str) -> dict:
    """Return per-ReplicaSet request counts and percentages from `increase(...)` results."""
    if payload.get("status") != "success":
        raise ValueError("Prometheus query failed")
    counts: dict[str, float] = {}
    for item in payload["data"]["result"]:
        counts[item["metric"].get("rollouts_pod_template_hash", "unlabeled")] = float(item["value"][1])
    total = sum(counts.values())
    if total <= 0:
        raise ValueError("No prediction requests were observed in the window")
    rows = []
    for hash_, count in sorted(counts.items(), key=lambda pair: -pair[1]):
        role = "canary" if hash_ == current and current != stable else "stable" if hash_ == stable else "other"
        rows.append({"hash": hash_, "role": role, "requests": round(count, 1),
                     "share_percent": round(100 * count / total, 2)})
    return {"total_requests": round(total, 1), "replicasets": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stable", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--window", required=True)
    args = parser.parse_args()
    try:
        result = shares(json.load(sys.stdin), args.stable, args.current)
    except ValueError as error:
        sys.exit(f"error: {error}")
    result["window"] = args.window
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
