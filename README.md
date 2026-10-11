# k6 load testing

`main.js` is a k6 workload: one virtual user creates an interview, answers it over SSE, finishes it,
reads the review, then walks the rest of the signed-in surface. `sweep.py` drives the same workload as
a ladder, deletes the run's rows between steps and writes one CSV row per level.

The question, the phases, the gates and the decisions are in `SRE.md`, the experiment's design doc,
which lives outside this repo. Code comments defer to it. This file owns how to run.

## Requirements

- k6 on PATH (`--k6` overrides the binary).
- A target that can take load without auth, vendor cost or a rate limit. `setup()` refuses to start
  unless the identity it reads is on the tier and the model the experiment expects.
- A sweep identity of its own, passed as `--sweep-sub`, so the rows the cleanup deletes are known to be
  the run's.

## Configure

Copy `.env.example` to `.env` and fill it in. Both entry points read it, and a value exported in the
shell wins over the file. k6 resolves the path against the directory you run from, so run from the
repo root.

## Run

```sh
k6 run main.js
python3 sweep.py --sweep-sub <account-id>
```

`python3 sweep.py --help` lists the flags.

## Output

`results/sweep-<utc>.csv`, one row per level, and `results/level-<n>.json`, the k6 summary each row is
read from. Both stay out of git.
