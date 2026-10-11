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

## Run

```sh
API_URL=https://host/api TOKEN=... k6 run main.js
TOKEN=... HOST_HEADER=... python3 sweep.py --api-url https://host/api --sweep-sub <account-id>
```

`python3 sweep.py --help` lists the flags.

## Env

| Variable                          | Default       | Meaning                                                                                          |
| --------------------------------- | ------------- | ------------------------------------------------------------------------------------------------ |
| `API_URL`                         | required      | Base URL including the API path prefix.                                                          |
| `TARGET_VUS`                      | 25            | Virtual users.                                                                                   |
| `TURNS_MIN` / `TURNS_MAX`         | 2 / 8         | Answers per interview.                                                                           |
| `THINK_MIN_S` / `THINK_MAX_S`     | 1 / 3         | Pause between answers.                                                                           |
| `TOKEN`                           | unset         | Bearer token, for a target that wants one.                                                       |
| `HOST_HEADER`                     | unset         | `Host` override, for an ingress that routes by host.                                             |
| `SUMMARY_PATH`                    | unset         | Where `handleSummary` writes the k6 JSON.                                                        |
| `RAMP_S` / `WARM_S` / `MEASURE_S` | 30 / 60 / 300 | Phase lengths. The driver sets these for a harness check; a run for the record leaves them unset. |

## Output

`results/sweep-<utc>.csv`, one row per level, and `results/level-<n>.json`, the k6 summary each row is
read from. Both stay out of git.
