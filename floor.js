// AI HERE: the floor is pinned to FLOOR_RPS; derive it from the last boundary row when a probe needs
// a different rate than the sweep's.
import http from "k6/http";
import { Counter } from "k6/metrics";

const API = (__ENV.API_URL || "").replace(/\/$/, "");
const RATE = Number(__ENV.FLOOR_RPS || 70);
const MEASURE_S = Number(__ENV.MEASURE_S || 120);
const COUNTED = new Counter("measure_requests");
const MEASURE_ITERS = new Counter("measure_iterations");

export const options = {
    scenarios: {
        measure: {
            executor: "constant-arrival-rate",
            rate: RATE,
            timeUnit: "1s",
            duration: `${MEASURE_S}s`,
            preAllocatedVUs: 10,
            maxVUs: 100,
            tags: { phase: "measure" },
        },
    },
    summaryTrendStats: ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"],
};

export default function () {
    MEASURE_ITERS.add(1);
    COUNTED.add(1);
    http.get(`${API}/health`, { tags: { name: "GET /api/health", model: "false" } });
}

export function handleSummary(data) {
    if (!__ENV.SUMMARY_PATH) return {};
    const counted = data.metrics.measure_requests?.values?.count;
    const rps = counted ? +(counted / MEASURE_S).toFixed(1) : null;
    const duration_ms = data.state?.testRunDurationMs;
    return {
        [__ENV.SUMMARY_PATH]: JSON.stringify({
            ...data,
            rps,
            measure_start_ms: duration_ms ? Date.now() - duration_ms : null,
        }),
    };
}
