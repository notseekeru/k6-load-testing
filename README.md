# k6 load harness

One virtual user walks the whole surface: it creates a run, starts it, opens it, answers turn by turn
over SSE, finishes it, reads the review, then works through the read endpoints. `sweep.py` drives that
workload as a sweep: a mandatory 10 VU smoke first, which aborts the run if it fails, then a ladder from
100 VUs that doubles, brackets the first level that fails, binary searches the gap below it down to the
smoke's 10 VUs, then re-runs the highest passing level once for `CONFIRM_MEASURE_S`; a failure there ends
the sweep. One CSV row per window, tagged `smoke`, `ladder`, `search`, `confirm`, or `only`. The run's rows
are deleted between levels, so every window starts from the same empty account.

## Layout

| File           | What                                        |
| -------------- | ------------------------------------------- |
| `main.js`      | the workload, its checks and its thresholds |
| `sweep.py`     | the ladder driver                           |
| `flake.nix`    | k6 and python3 on PATH                      |
| `.env.example` | every value either entry point reads        |

## Requirements

- k6 and python3 on PATH, or `nix develop`, which puts both there.
- A target that takes load without auth, vendor cost or a rate limit. `main.js` refuses to start
  otherwise, before the loop sends anything.
- An identity of its own for the sweep account, so the cleanup deletes only the run's rows. On the
  target that is its dev or test identity; here it is `--sweep-sub` or `SWEEP_SUB`.

## Configure

Copy `.env.example` to `.env` and fill it in. A value exported in the shell wins over the file, and
`--api-url` wins over both. k6 resolves `.env` against the directory you run from, so run from the repo
root. The template carries every value and what it does.

## Run

```sh
nix develop
k6 run main.js                                          # one level, TARGET_VUS users
python3 sweep.py --sweep-sub <id>                       # smoke, then the ladder, cleanup between levels
python3 sweep.py --sweep-sub <id> --only 10 --smoke     # that same smoke, without the ladder
python3 sweep.py --sweep-sub <id> --cleanup-only        # delete the run's rows, load nothing
```

`python3 sweep.py --help` lists the flags.

## Output

`results/sweep-<utc>.csv`, one row per window: its label, phase and window length, p50, p95, p99, max, error
rate, iterations, requests, rps of the measurement window, seconds and the account's row count before
and after the window. `pool_wait_*`, `query_*`, `pool_size_max` and `pool_idle_min` are read from
Grafana's Prometheus over the same window; a read that fails stays blank with its reason in `db_note`.
The label is `--label` or `LABEL`, so a file names the build, pool and path it measured.

`run_id` is the same stamp as the run's `results/level-<n>-<phase>-<run_id>.json`, so a row finds its summary
in one lookup. `measure_start` is the UTC start of the measurement window, so a row lines up with Grafana
without knowing the run's timings. Both results files stay out of git.
