# k6 load testing

`main.js` is a k6 workload: one virtual user creates an interview, answers it over SSE, finishes it,
reads the review, then walks the rest of the signed-in surface. `sweep.py` drives the same workload as
a ladder, deletes the run's rows between steps and writes one CSV row per level.

The question, the phases, the gates and the decisions are in `SRE-LOADTESTING.md`, the experiment's design doc,
which lives outside this repo. Code comments defer to it. This file owns how to run.

## Requirements

- k6 on PATH (`--k6` overrides the binary).
- A target that can take load without auth, vendor cost or a rate limit. `setup()` refuses to start
  unless the identity it reads is on the tier and the model the experiment expects.
- A sweep identity of its own, passed as `--sweep-sub`, so the rows the cleanup deletes are known to be
  the run's. Set the target's `DEV_USER_SUB` to that same value, or the sweep spends your own dev
  account's rows and daily turn counter instead of its own.

## Configure

Copy `.env.example` to `.env` and fill it in. Both entry points read it, and a value exported in the
shell wins over the file. k6 resolves the path against the directory you run from, so run from the
repo root.

## Run

The sweep runs against the perf window, which is a gitops edit: swap
`apps/maxterview/overlays/prod` for `apps/maxterview/overlays/perf` in `apps-of-apps/maxterview.yaml`,
wait for the sync, and confirm the pod answers `tier=dev` before any load. `setup()` refuses to start
otherwise, so a window that never opened cannot be measured by accident. Swap the path back when the
ladder stops: the window turns Clerk and the app's rate limit off, and it must not outlive the run.

```sh
k6 run main.js
python3 sweep.py --sweep-sub <account-id>
```

Without k6 installed, `nix shell nixpkgs#k6 -c python3 sweep.py --sweep-sub <account-id>` runs the same
driver, and `nix run nixpkgs#k6 -- run main.js` the workload on its own.

`python3 sweep.py --help` lists the flags.

## Output

`results/sweep-<utc>.csv`, one row per level, and `results/level-<n>.json`, the k6 summary each row is
read from. Both stay out of git.
