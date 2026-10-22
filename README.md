# k6 load harness

One virtual user walks the whole surface: it creates a run, starts it, opens it, answers turn by turn
over SSE, finishes it, reads the review, then works through the read endpoints. `sweep.py` drives that
workload as a ladder: it climbs from the first level, brackets the first level that fails, binary
searches the gap below it, re-runs the highest passing level three times, and writes one CSV row per
level. The run's rows are deleted between levels, so every window starts from the same empty account.

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
python3 sweep.py --sweep-sub <id>                       # the ladder, with cleanup between levels
python3 sweep.py --sweep-sub <id> --only 10 --smoke     # one short level, for a harness check
python3 sweep.py --sweep-sub <id> --cleanup-only        # delete the run's rows, load nothing
```

`python3 sweep.py --help` lists the flags.

## Output

`results/sweep-<utc>.csv`, one row per window: its phase and window length, p50, p95, p99, error rate,
iterations, requests, rps of the measurement window, seconds and the account's row count before and
after the window.
`results/level-<n>.json` is the k6 summary each row is read from. Both stay out of git.
