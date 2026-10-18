#!/usr/bin/env python3
"""Ladder driver for the k6 sweep (SRE-LOADTESTING.md).

Levels double. A failure in the first level stops the sweep: that is a broken test or a broken app, not
load. The first failure above it is the top of the range, and the gap below is binary-searched for the
highest level that still passes.

Rows are deleted between steps through the API, so the account the loop reads stays the same size and
the curve is not partly made of the test's own writes. The cleanup refuses to touch anything unless
every interview the API returns belongs to --sweep-sub.

Runbook and flags: README. TOKEN and HOST_HEADER come from .env or the environment and reach both this
script and k6.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

SMOKE_LEVELS = 1
CONFIRM_RUNS = 3
GAP_S = 15
DEFAULT_MAX_VUS = 800
DELETE_WORKERS = 32
# One level is the doc's phases plus the measurement window's graceful stop and k6's own startup.
LEVEL_TIMEOUT_S = 900
RESULTS_DIR = Path(__file__).with_name("results")
ENV_FILE = Path(__file__).with_name(".env")


# AI HERE: KEY=value only, no export prefix, quotes or interpolation.
def env_file():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#"):
                env[key.strip()] = value.strip()
    return env


DOTENV = env_file()


def cfg(key, fallback=""):
    return os.getenv(key) or DOTENV.get(key) or fallback


TOKEN = cfg("TOKEN")
HOST_HEADER = cfg("HOST_HEADER")


def call(method, url, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if TOKEN:
        request.add_header("Authorization", f"Bearer {TOKEN}")
    # The same Host override k6 sends: an ingress that routes by host answers 404 without it.
    if HOST_HEADER:
        request.add_header("Host", HOST_HEADER)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        return response.status, (json.loads(raw) if raw else None)


# The sweep identity is decision 10 in SRE-LOADTESTING.md: its own rows, its own turn counter, so a delete set
# is known to be the run's. Any foreign owner here means the identity is wrong and the abort is the
# only safe move: this deletes prod's rows.
def cleanup(api_url, sweep_sub):
    try:
        _, interviews = call("GET", f"{api_url}/interviews")
    except Exception as exc:
        sys.exit(f"ABORT: cannot list interviews at {api_url}: {exc}. Nothing to delete, nothing to load.")
    interviews = interviews or []
    owners = {row.get("owner_id") for row in interviews}
    if owners - {sweep_sub}:
        sys.exit(f"ABORT: /interviews returned owners {sorted(owners)}; expected only {sweep_sub}")
    if not interviews:
        print("cleanup: nothing to delete")
        return 0

    deleted = 0
    failed = 0

    def drop(interview_id):
        try:
            call("DELETE", f"{api_url}/interviews/{interview_id}")
            return 1
        except urllib.error.HTTPError as exc:
            return 0 if exc.code == 404 else -1
        except Exception:
            return -1

    with ThreadPoolExecutor(max_workers=DELETE_WORKERS) as pool:
        for result in pool.map(drop, [row["id"] for row in interviews]):
            if result == 1:
                deleted += 1
            elif result == -1:
                failed += 1
    print(f"cleanup: {deleted} deleted, {failed} failed, owner={sweep_sub}")
    if failed:
        sys.exit("ABORT: cleanup left rows behind, the next step would read a bigger account than the last")
    return deleted


def run_level(level, args, deleted):
    summary_path = RESULTS_DIR / f"level-{int(level)}.json"
    # Left over from an earlier run of this level, it would be read as this run's numbers.
    summary_path.unlink(missing_ok=True)
    env = {
        **DOTENV,
        **os.environ,
        "API_URL": args.api_url,
        "TARGET_VUS": str(int(level)),
        "SUMMARY_PATH": str(summary_path),
    }
    if args.smoke:
        env.update(RAMP_S="3", WARM_S="3", MEASURE_S="10")

    started = time.time()
    completed = subprocess.run(
        [args.k6, "run", str(Path(__file__).with_name("main.js"))],
        env=env,
        capture_output=True,
        text=True,
        timeout=LEVEL_TIMEOUT_S,
    )
    elapsed = time.time() - started

    if completed.returncode not in (0, 99):
        print(completed.stdout[-4000:])
        print(completed.stderr[-4000:], file=sys.stderr)
        sys.exit(f"ABORT: k6 exited {completed.returncode}: harness, script or target is not runnable")

    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    metrics = summary.get("metrics", {})
    # k6 exits 99 on a failed threshold, so the exit code is the verdict.
    passed = completed.returncode == 0
    measured = measured_metrics(metrics)
    return {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "vus": int(level),
        "verdict": "pass" if passed else "fail",
        "p50_ms": measured.get("p50"),
        "p95_ms": measured.get("p95"),
        "p99_ms": measured.get("p99"),
        "err_rate": measured.get("err_rate"),
        "iterations": measured.get("iterations"),
        "http_reqs": measured.get("http_reqs"),
        "seconds": round(elapsed, 1),
        "deleted_before": deleted,
    }


# The exported digest carries p(95) but not always p(99), so p99 falls back to max. A metric that is
# absent stays blank: a zero would read as a perfect number.
def measured_metrics(metrics):
    duration = pick_metric(metrics, "http_req_duration", ("phase:measure", "model:false"))
    failed = pick_metric(metrics, "http_req_failed")
    return {
        "p50": rounded(duration.get("med")),
        "p95": rounded(duration.get("p(95)", duration.get("med"))),
        "p99": rounded(duration.get("p(99)", duration.get("max"))),
        "err_rate": rounded(failed.get("rate"), 5),
        "iterations": int(metrics.get("iterations", {}).get("values", {}).get("count", 0)),
        "http_reqs": int(metrics.get("http_reqs", {}).get("values", {}).get("count", 0)),
    }


def rounded(value, places=1):
    return None if value is None else round(value, places)


# phase:measure only: the untagged metric folds the ramp and the warm-up into the same number.
def pick_metric(metrics, name, tags=("phase:measure",)):
    for key, value in metrics.items():
        if key.startswith(f"{name}{{") and all(tag in key for tag in tags):
            return value.get("values", {})
    return {}


def main():
    parser = argparse.ArgumentParser(description="k6 sweep ladder driver")
    parser.add_argument("--api-url", default="", help="e.g. https://host/api; API_URL from .env when unset")
    parser.add_argument("--sweep-sub", required=True, help="the sweep owner_id every listed row must have")
    parser.add_argument("--k6", default="k6")
    parser.add_argument("--max-vus", type=int, default=DEFAULT_MAX_VUS, help="top of the ladder; doubling stops here")
    parser.add_argument("--smoke", action="store_true", help="shorten the phases for a harness check")
    parser.add_argument("--no-cleanup", action="store_true", help="keep rows between steps (do not)")
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--only", type=int, help="run one level and stop")
    args = parser.parse_args()

    args.api_url = (args.api_url or cfg("API_URL")).rstrip("/")
    if not args.api_url:
        sys.exit("ABORT: --api-url or API_URL in .env is required")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    self_check()

    # Only the cleanup path needs a real identity; with --no-cleanup nothing is deleted, so a harness
    # check may run as dev-user.
    if (not args.sweep_sub or args.sweep_sub == "dev-user") and not args.no_cleanup:
        sys.exit("ABORT: --sweep-sub must name the sweep identity (DEV_USER_SUB), not dev-user")

    if args.cleanup_only:
        cleanup(args.api_url, args.sweep_sub)
        return

    if args.only:
        rows = run_one(args.only, args)
        print(f"{args.only} VUs: {rows['verdict']} p95={rows['p95_ms']}ms p99={rows['p99_ms']}ms err={rows['err_rate']}")
        return

    levels = ladder(args.max_vus)
    csv_path = RESULTS_DIR / f"sweep-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.csv"
    print(f"ladder: {levels} (max {args.max_vus}), csv {csv_path}")

    last_pass = 0
    first_fail = None

    for index, vus in enumerate(levels):
        row = run_step(csv_path, vus, args, between=index > 0)
        print(f"{vus} VUs: {row['verdict']} p95={row['p95_ms']}ms p99={row['p99_ms']}ms err={row['err_rate']}")
        if row["verdict"] == "pass":
            last_pass = vus
            continue
        if index < SMOKE_LEVELS:
            sys.exit(f"ABORT: {vus} VUs failed during the smoke checks. That is not a load problem: fix the test or the app.")
        first_fail = vus
        break

    if first_fail is None:
        print(f"no failure up to {last_pass} VUs; raise --max-vus")
        return

    low, high = last_pass, first_fail
    gap = tolerance(low)
    while high - low > gap:
        mid = (low + high) // 2
        row = run_step(csv_path, mid, args, between=True)
        print(f"{mid} VUs (search {low}-{high}): {row['verdict']}")
        if row["verdict"] == "pass":
            low = mid
        else:
            high = mid

    print(f"boundary: {low} VUs pass, {high} VUs fail (gap under {gap}); csv {csv_path}")

    passed = 0
    for run in range(CONFIRM_RUNS):
        row = run_step(csv_path, low, args, between=True)
        passed += row["verdict"] == "pass"
        print(f"{low} VUs (confirm {run + 1}/{CONFIRM_RUNS}): {row['verdict']}")
    print(f"confirmed {low} VUs: {passed}/{CONFIRM_RUNS} passed")


def ladder(max_vus):
    levels, level = [], 100
    while level <= max_vus:
        levels.append(level)
        level *= 2
    return levels


def tolerance(low):
    return max(25, int(0.1 * low))


# Framework-free and run before any request leaves the box: the ladder and the tolerance are what the
# search depends on.
def self_check():
    levels = ladder(DEFAULT_MAX_VUS)
    assert levels == [100, 200, 400, 800], levels
    assert tolerance(levels[SMOKE_LEVELS - 1]) < levels[SMOKE_LEVELS] - levels[SMOKE_LEVELS - 1]
    assert GAP_S * 2 + 30 <= 60, "the gap has to stay inside asyncpg's idle close and Neon's scale-to-zero"


def account_size(api_url):
    try:
        _, interviews = call("GET", f"{api_url}/interviews")
        return len(interviews or [])
    except Exception:
        return None


def run_one(vus, args, between=False):
    if between:
        time.sleep(GAP_S)
    deleted = 0 if args.no_cleanup else cleanup(args.api_url, args.sweep_sub)
    if between:
        time.sleep(GAP_S)
    row = run_level(vus, args, deleted)
    row["rows_after"] = account_size(args.api_url)
    return row


def run_step(csv_path, vus, args, between=False):
    row = run_one(vus, args, between)
    with csv_path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, row.keys())
        if handle.tell() == 0:
            writer.writeheader()
        writer.writerow(row)
    return row


if __name__ == "__main__":
    main()
