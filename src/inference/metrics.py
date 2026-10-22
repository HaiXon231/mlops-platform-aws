"""Small bounded Prometheus text exporter for a single Uvicorn worker."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


class Metrics:
    def __init__(self) -> None:
        self.lock = Lock()
        self.counts: dict[tuple[str, str, str], int] = defaultdict(int)
        self.errors: dict[tuple[str, str, str], int] = defaultdict(int)
        self.duration_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * (len(BUCKETS) + 1))
        self.duration_sums: dict[tuple[str, str], float] = defaultdict(float)
        self.model: tuple[str, str, str] | None = None

    def observe(self, method: str, path: str, status: int, seconds: float) -> None:
        key = (method, path, str(status))
        with self.lock:
            self.counts[key] += 1
            if status >= 400:
                self.errors[key] += 1
            duration_key = (method, path)
            for index, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    self.duration_counts[duration_key][index] += 1
            self.duration_counts[duration_key][-1] += 1
            self.duration_sums[duration_key] += seconds

    def render(self) -> str:
        lines = ["# HELP inference_http_requests_total HTTP requests", "# TYPE inference_http_requests_total counter"]
        with self.lock:
            for (method, path, status), count in sorted(self.counts.items()):
                lines.append(f'inference_http_requests_total{{method="{method}",path="{path}",status="{status}"}} {count}')
            lines.extend(("# HELP inference_http_errors_total HTTP error responses", "# TYPE inference_http_errors_total counter"))
            for (method, path, status), count in sorted(self.errors.items()):
                lines.append(f'inference_http_errors_total{{method="{method}",path="{path}",status="{status}"}} {count}')
            lines.extend(("# HELP inference_http_request_duration_seconds HTTP request latency", "# TYPE inference_http_request_duration_seconds histogram"))
            for (method, path), counts in sorted(self.duration_counts.items()):
                labels = f'method="{method}",path="{path}"'
                for index, bound in enumerate(BUCKETS):
                    lines.append(f'inference_http_request_duration_seconds_bucket{{{labels},le="{bound}"}} {counts[index]}')
                lines.append(f'inference_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {counts[-1]}')
                lines.append(f'inference_http_request_duration_seconds_sum{{{labels}}} {self.duration_sums[(method, path)]}')
                lines.append(f'inference_http_request_duration_seconds_count{{{labels}}} {counts[-1]}')
            lines.extend(("# HELP inference_model_info Loaded release", "# TYPE inference_model_info gauge"))
            if self.model:
                name, version, run_id = self.model
                # These identifiers come from validated release metadata, never from a request.
                lines.append(f'inference_model_info{{model_name="{name}",model_version="{version}",source_run_id="{run_id}"}} 1')
        return "\n".join(lines) + "\n"
