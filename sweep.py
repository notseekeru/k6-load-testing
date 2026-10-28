#!/usr/bin/env python3
"""Ladder driver for the k6 sweep (SRE.md).

Levels double. The smoke is the gate: a failure there stops the sweep because it is a broken test or a
broken app, not load. The first failure above the smoke is the top of the range, and the gap below is
binary-searched for the highest level that still passes.

Rows are deleted between levels through the API, so the account the loop reads stays the same size and
the curve is not partly made of the test's own writes. The cleanup refuses to touch anything unless
every interview the API returns belongs to --sweep-sub.

Runbook and flags: README. TOKEN and HOST_HEADER come from .env or the environment and reach both this
script and k6. GRAFANA_URL, GRAFANA_SERVICE_ACCOUNT_TOKEN, PROM_DS and APP_LABEL feed the per-window db
read and stay in this process.
"""

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path

SMOKE_VUS = 10
CONFIRM_MEASURE_S = 300
GAP_S = 15
CLEANUP_BUDGET_S = 30
DEFAULT_START = 100
DEFAULT_MAX_VUS = 800
DELETE_WORKERS = 32
# The metric exporter flushes on a timer, so the window's last samples land after k6 exits.
# AI HERE: a fixed settle; poll the query/histogram count if a run ever needs to finish faster.
GRAFANA_SETTLE_S = 80
# One level is the k6 scenarios plus the measurement window's graceful stop and k6's own startup.
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
GRAFANA_URL = cfg("GRAFANA_URL", "https://sturdypoplar3317.grafana.net").rstrip("/")
GRAFANA_TOKEN = cfg("GRAFANA_SERVICE_ACCOUNT_TOKEN")
PROM_DS = cfg("PROM_DS", "grafanacloud-prom")
APP_LABEL = cfg("APP_LABEL", "maxterview-backend")


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


def all_interviews(api_url):
    rows, offset, limit = [], 0, 200
    while True:
        _, page = call("GET", f"{api_url}/interviews?limit={limit}&offset={offset}")
        page = page or []
        # A short page is the last one. A long one means the target ignores limit/offset, so it is
        # also the last one.
        if len(page) != limit:
            return rows + page
        rows.extend(page)
        offset += limit


# The sweep identity is decision 10 in SRE.md: its own rows, its own turn counter, so a delete set
# is known to be the run's. Any foreign owner here means the identity is wrong and the abort is the
# only safe move: this deletes prod's rows.
def cleanup(api_url, sweep_sub):
    try:
        interviews = all_interviews(api_url)
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

    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=DELETE_WORKERS)
    try:
        pending = {pool.submit(drop, row["id"]) for row in interviews}
        done, pending = wait(pending, timeout=CLEANUP_BUDGET_S)
        for future in done:
            result = future.result()
            if result == 1:
                deleted += 1
            elif result == -1:
                failed += 1
        for future in pending:
            future.cancel()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    elapsed = time.monotonic() - started
    print(f"cleanup: {deleted} deleted, {failed} failed, {len(pending)} left behind, {elapsed:.1f}s of {CLEANUP_BUDGET_S}s, owner={sweep_sub}")
    if failed or pending:
        sys.exit("ABORT: cleanup left rows behind, the next level would read a bigger account than the last")
    return deleted


def run_level(level, args, deleted, measure_s=None, phase="ladder", smoke=False):
    # One file per run, so a level's ladder, search and confirm summaries stay apart.
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    summary_path = RESULTS_DIR / f"level-{int(level)}-{phase}-{run_id}.json"
    env = {
        **DOTENV,
        **os.environ,
        "API_URL": args.api_url,
        "TARGET_VUS": str(int(level)),
        "SUMMARY_PATH": str(summary_path),
    }
    if args.smoke or smoke:
        env.update(RAMP_S="3", WARM_S="3", MEASURE_S="10")
    if measure_s:
        env["MEASURE_S"] = str(int(measure_s))
    # k6 has no use for the Grafana token; keep it out of the child's environment.
    env.pop("GRAFANA_SERVICE_ACCOUNT_TOKEN", None)

    started = time.time()
    window_start = datetime.fromtimestamp(
        started + int(env.get("RAMP_S", 30)) + int(env.get("WARM_S", 60)), UTC
    ).isoformat(timespec="seconds")
    try:
        completed = subprocess.run(
            [args.k6, "run", str(Path(__file__).with_name("main.js"))],
            env=env,
            capture_output=True,
            text=True,
            timeout=LEVEL_TIMEOUT_S,
        )
    except FileNotFoundError:
        sys.exit(f"ABORT: cannot run '{args.k6}'. Install k6, or run inside 'nix develop' or direnv.")
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
    db = db_metrics(window_start, int(env["MEASURE_S"]))
    return {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_id": run_id,
        "label": args.label,
        "measure_start": window_start,
        "phase": phase,
        "vus": int(level),
        "measure_s": int(env["MEASURE_S"]),
        "verdict": "pass" if passed else "fail",
        "p50_ms": measured.get("p50"),
        "p95_ms": measured.get("p95"),
        "p99_ms": measured.get("p99"),
        "max_ms": measured.get("max"),
        "err_rate": measured.get("err_rate"),
        "iterations": measured.get("iterations"),
        "http_reqs": measured.get("http_reqs"),
        "rps": summary.get("rps"),
        "seconds": round(elapsed, 1),
        "deleted_before": deleted,
        **db,
    }


DB_COLUMNS = (
    "pool_wait_p50_ms",
    "pool_wait_p95_ms",
    "query_p50_ms",
    "query_p95_ms",
    "query_mean_ms",
    "pool_size_max",
    "pool_idle_min",
)


# The server side of the same window: the wait apart from the SQL, and how full the pool ran. A read
# that fails leaves the columns blank and says why in db_note; the harness verdict is unchanged.
def db_metrics(measure_start, measure_s):
    blank = {**dict.fromkeys(DB_COLUMNS), "db_note": ""}
    if not GRAFANA_TOKEN:
        blank["db_note"] = "not recorded: GRAFANA_SERVICE_ACCOUNT_TOKEN unset"
        return blank
    end = datetime.fromisoformat(measure_start).timestamp() + measure_s
    settle = end + GRAFANA_SETTLE_S - time.time()
    if settle > 0:
        time.sleep(settle)
    svc = f'{{service_name="{APP_LABEL}"}}'
    win = f"[{int(measure_s)}s]"
    hist = lambda metric, quantile: (
        f"histogram_quantile({quantile}, sum by (le) (increase({metric}_bucket{svc}{win})))"
    )
    queries = {
        "pool_wait_p50_ms": (hist("db_pool_wait_duration_seconds", 0.5), 1000),
        "pool_wait_p95_ms": (hist("db_pool_wait_duration_seconds", 0.95), 1000),
        "query_p50_ms": (hist("db_query_duration_seconds", 0.5), 1000),
        "query_p95_ms": (hist("db_query_duration_seconds", 0.95), 1000),
        "query_mean_ms": (
            f"sum(increase(db_query_duration_seconds_sum{svc}{win})) / sum(increase(db_query_duration_seconds_count{svc}{win}))",
            1000,
        ),
        "pool_size_max": (f"max(max_over_time(db_pool_size_ratio{svc}{win}))", 1),
        "pool_idle_min": (f"min(min_over_time(db_pool_idle_ratio{svc}{win}))", 1),
    }

    def one(item):
        key, (expr, scale) = item
        try:
            value = prom(expr, end)
        except Exception as exc:
            return key, None, f"{key}: {exc}"
        if value is None or not math.isfinite(value):
            return key, None, f"{key}: no sample"
        return key, round(value * scale, 1), None

    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        results = list(pool.map(one, queries.items()))
    row = {key: value for key, value, _ in results}
    notes = list(dict.fromkeys(note for _, _, note in results if note))
    if notes:
        missing = sum(1 for _, value, _ in results if value is None)
        row["db_note"] = ("not recorded: " if missing == len(results) else "partial: ") + "; ".join(notes)
    else:
        row["db_note"] = ""
    return row


# One instant query through Grafana's Prometheus proxy, at the end of the window.
def prom(expr, at):
    url = (
        f"{GRAFANA_URL}/api/datasources/proxy/uid/{urllib.parse.quote(PROM_DS)}/api/v1/query?"
        + urllib.parse.urlencode({"query": expr, "time": int(at)})
    )
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {GRAFANA_TOKEN}")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read())
    rows = payload.get("data", {}).get("result") or []
    return float(rows[0]["value"][1]) if rows else None


# A metric that is absent stays blank: a zero would read as a perfect number.
def measured_metrics(metrics):
    duration = pick_metric(metrics, "http_req_duration", ("phase:measure", "model:false"))
    failed = pick_metric(metrics, "http_req_failed")
    return {
        "p50": rounded(duration.get("med")),
        "p95": rounded(duration.get("p(95)")),
        "p99": rounded(duration.get("p(99)")),
        "max": rounded(duration.get("max")),
        "err_rate": rounded(failed.get("rate"), 5),
        "iterations": int(metrics.get("iterations", {}).get("values", {}).get("count", 0)),
        "http_reqs": int(metrics.get("http_reqs", {}).get("values", {}).get("count", 0)),
    }


def rounded(value, places=1):
    return None if value is None else round(value, places)


def ms(value):
    return "n/a" if value is None else f"{value}ms"


# phase:measure only: the untagged metric folds the ramp and the warm-up into the same number.
def pick_metric(metrics, name, tags=("phase:measure",)):
    for key, value in metrics.items():
        if key.startswith(f"{name}{{") and all(tag in key for tag in tags):
            return value.get("values", {})
    return {}


def main():
    parser = argparse.ArgumentParser(description="k6 sweep ladder driver")
    parser.add_argument("--api-url", default="", help="e.g. https://host/api; API_URL from .env when unset")
    parser.add_argument("--sweep-sub", default="", help="the sweep owner_id every listed row must have; SWEEP_SUB from .env when unset")
    parser.add_argument("--k6", default="k6")
    parser.add_argument("--label", default="", help="a short tag for the configuration under test, written to every row")
    parser.add_argument("--start", type=int, default=DEFAULT_START, help="first level of the ladder, doubling from there")
    parser.add_argument("--max-vus", type=int, default=DEFAULT_MAX_VUS, help="top of the ladder; doubling stops here")
    parser.add_argument("--smoke", action="store_true", help="shorten every window, for --only")
    parser.add_argument("--no-cleanup", action="store_true", help="keep rows between levels (do not)")
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--only", type=int, help="run one level and stop")
    args = parser.parse_args()

    args.api_url = (args.api_url or cfg("API_URL")).rstrip("/")
    args.sweep_sub = args.sweep_sub or cfg("SWEEP_SUB")
    args.label = args.label or cfg("LABEL")
    if not args.api_url:
        sys.exit("ABORT: --api-url or API_URL in .env is required")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    self_check()
    csv_path = RESULTS_DIR / f"sweep-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.csv"
    if args.start < 1:
        sys.exit("ABORT: --start must be at least 1, or the ladder never advances")

    # Only the cleanup path needs a real identity; with --no-cleanup nothing is deleted, so a harness
    # check may run as dev-user.
    if (not args.sweep_sub or args.sweep_sub == "dev-user") and not args.no_cleanup:
        sys.exit("ABORT: --sweep-sub or SWEEP_SUB must name the sweep identity (DEV_USER_SUB), not dev-user")

    if args.cleanup_only:
        cleanup(args.api_url, args.sweep_sub)
        return

    if args.smoke and not args.only:
        sys.exit(f"ABORT: --smoke shortens every window; it is for --only. The sweep opens with its own {SMOKE_VUS} VU smoke.")

    if args.only:
        row = record_level(csv_path, args.only, args, phase="only")
        print(f"{args.only} VUs: {row['verdict']} p95={ms(row['p95_ms'])} p99={ms(row['p99_ms'])} err={row['err_rate']}; csv {csv_path}")
        return

    if args.start <= SMOKE_VUS:
        sys.exit(f"ABORT: --start must be above the {SMOKE_VUS} VU smoke, or the ladder cannot bracket it")
    levels = ladder(args.start, args.max_vus)
    if not levels:
        sys.exit(f"ABORT: --start {args.start} is above --max-vus {args.max_vus}, so the ladder is empty")
    print(f"ladder: {levels} (max {args.max_vus}), csv {csv_path}")
    run_sweep(csv_path, args, levels)


def run_sweep(csv_path, args, levels):
    smoke = record_level(csv_path, SMOKE_VUS, args, phase="smoke", smoke=True)
    print(f"{SMOKE_VUS} VUs smoke: {smoke['verdict']} p95={ms(smoke['p95_ms'])} err={smoke['err_rate']}")
    if smoke["verdict"] != "pass":
        sys.exit(f"ABORT: the {SMOKE_VUS} VU smoke failed. The harness or the app is broken before any load: fix it, then run again.")

    last_pass = SMOKE_VUS
    first_fail = None

    for vus in levels:
        row = record_level(csv_path, vus, args, between=True, phase="ladder")
        print(f"{vus} VUs: {row['verdict']} p95={ms(row['p95_ms'])} p99={ms(row['p99_ms'])} err={row['err_rate']}")
        if row["verdict"] == "pass":
            last_pass = vus
            continue
        first_fail = vus
        break

    if first_fail is None:
        print(f"no failure up to {last_pass} VUs; raise --max-vus")
        return None

    low, high = search(csv_path, last_pass, first_fail, args)
    print(f"boundary: {low} VUs pass, {high} VUs fail; csv {csv_path}")

    row = record_level(csv_path, low, args, between=True, measure_s=CONFIRM_MEASURE_S, phase="confirm")
    print(f"{low} VUs (confirm {CONFIRM_MEASURE_S}s): {row['verdict']}")
    if row["verdict"] != "pass":
        sys.exit(f"ABORT: {low} VUs passed the search and failed its {CONFIRM_MEASURE_S}s run; the boundary is unproven, csv {csv_path}")
    print(f"confirmed {low} VUs in one {CONFIRM_MEASURE_S}s run; csv {csv_path}")
    return low


def search(csv_path, low, high, args):
    while high - low > tolerance(low):
        mid = (low + high) // 2
        row = record_level(csv_path, mid, args, between=True, phase="search")
        print(f"{mid} VUs (search {low}-{high}): {row['verdict']}")
        if row["verdict"] == "pass":
            low = mid
        else:
            high = mid
    return low, high


def ladder(start, max_vus):
    levels, level = [], start
    while level <= max_vus:
        levels.append(level)
        level *= 2
    return levels


def tolerance(low):
    return max(25, int(0.1 * low))


# Framework-free and run before any request leaves the box: the ladder and the tolerance are what the
# search depends on.
def self_check():
    levels = ladder(DEFAULT_START, DEFAULT_MAX_VUS)
    assert levels == [100, 200, 400, 800], levels
    assert ladder(10, 100) == [10, 20, 40, 80], ladder(10, 100)
    assert tolerance(SMOKE_VUS) < DEFAULT_START - SMOKE_VUS, "the search needs room between the smoke and the first level"
    assert GAP_S * 2 + CLEANUP_BUDGET_S <= 60, "the gap has to stay inside asyncpg's idle close and Neon's scale-to-zero"


def account_size(api_url):
    try:
        return len(all_interviews(api_url))
    except Exception:
        return None


def run_one(vus, args, between=False, measure_s=None, phase="ladder", smoke=False):
    if between:
        time.sleep(GAP_S)
    deleted = 0 if args.no_cleanup else cleanup(args.api_url, args.sweep_sub)
    if between:
        time.sleep(GAP_S)
    row = run_level(vus, args, deleted, measure_s, phase, smoke)
    row["rows_after"] = account_size(args.api_url)
    return row


def record_level(csv_path, vus, args, between=False, measure_s=None, phase="ladder", smoke=False):
    row = run_one(vus, args, between, measure_s, phase, smoke)
    with csv_path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, row.keys())
        if handle.tell() == 0:
            writer.writeheader()
        writer.writerow(row)
    return row


if __name__ == "__main__":
    main()
