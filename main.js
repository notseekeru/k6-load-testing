// SRE.md owns the question, the workload, the phases and the gates; this file implements them, so a
// change to any of those belongs there first. Runbook and env: README.

import { check, fail, sleep } from "k6";
import http from "k6/http";

// .env beside the script, read once at init. AI HERE: KEY=value only, no export prefix, quotes or
// interpolation. k6 resolves the path against the run directory, not this file.
const DOTENV = (() => {
    const env = {};
    try {
        for (const line of open(".env").split("\n")) {
            const at = line.indexOf("=");
            const key = line.slice(0, at).trim();
            if (at > 0 && !key.startsWith("#"))
                env[key] = line.slice(at + 1).trim();
        }
    } catch (e) {}
    return env;
})();

const cfg = (key, fallback) => __ENV[key] || DOTENV[key] || fallback;

const API = cfg("API_URL", "").replace(/\/+$/, "");
if (!API)
    throw new Error("API_URL is required: set it in .env or the environment");

const TARGET_VUS = num("TARGET_VUS", 25);
const TURNS_MIN = num("TURNS_MIN", 2);
const TURNS_MAX = num("TURNS_MAX", 8);
const THINK_MIN_S = num("THINK_MIN_S", 1);
const THINK_MAX_S = num("THINK_MAX_S", 3);
const ANSWER_MIN_CHARS = 200;
const ANSWER_MAX_CHARS = 1200;
const TOKEN = cfg("TOKEN", "");
const HOST_HEADER = cfg("HOST_HEADER", "");

const RAMP_S = num("RAMP_S", 30);
const WARM_S = num("WARM_S", 60);
const MEASURE_S = num("MEASURE_S", 300);

// SRE.md's gates, held here so k6 can enforce them.
const P95_MS = 1000;
const P99_MS = 5000;

export const options = {
    scenarios: {
        ramp_warm: {
            executor: "ramping-vus",
            startVUs: 0,
            stages: [
                { duration: `${RAMP_S}s`, target: TARGET_VUS },
                { duration: `${WARM_S}s`, target: TARGET_VUS },
            ],
            tags: { phase: "warm" },
            gracefulStop: `${RAMP_S}s`,
        },
        measure: {
            executor: "constant-vus",
            vus: TARGET_VUS,
            duration: `${MEASURE_S}s`,
            startTime: `${RAMP_S + WARM_S}s`,
            tags: { phase: "measure" },
            gracefulStop: "120s",
        },
    },
    thresholds: {
        "http_req_duration{phase:measure}": [
            `p(95)<${P95_MS}`,
            `p(99)<${P99_MS}`,
        ],
        "http_req_failed{phase:measure}": ["rate<0.01"],
        // Correctness is binary (SRE.md SLO). Rejected: rate>0.99, which passes while a bug bites only some
        // iterations.
        "checks{phase:measure}": ["rate==1"],
    },
};

// Asserted from the API rather than trusted: nothing loads unless the tier is dev and the model is
// the stub.
export function setup() {
    const usage = http.get(`${API}/usage`, params("GET /api/usage"));
    if (usage.status !== 200)
        fail(
            `GET /usage answered ${usage.status}: cannot confirm the perf window`,
        );
    const u = safe(usage);
    if (u.tier !== "dev")
        fail(
            `perf window not open: tier=${u.tier}, expected dev (CLERK_ENABLED=false, APP_ENV=test)`,
        );

    const provider = http.get(
        `${API}/settings/llm`,
        params("GET /api/settings/llm"),
    );
    if (provider.status !== 200)
        fail(`GET /settings/llm answered ${provider.status}`);
    const p = safe(provider);
    if (p.system.configured)
        fail(
            "perf window not open: a live model is configured, expected the stub (LLM_BASE_URL unset)",
        );
    if (p.byok)
        fail(
            "perf window not open: this identity saved a BYOK provider, expected the system default",
        );

    console.log(
        `window: tier=${u.tier} turns/day=${u[`chat_turns_per_day_${u.tier}`]} used=${u.chat_turns_used_today} model=stub vus=${TARGET_VUS}`,
    );
}

// Written where the driver points it: the summary format then does not depend on a k6 CLI flag.
export function handleSummary(data) {
    return __ENV.SUMMARY_PATH
        ? { [__ENV.SUMMARY_PATH]: JSON.stringify(data) }
        : {};
}

export default function () {
    const created = http.post(
        `${API}/interviews`,
        JSON.stringify(randomInterview()),
        params("POST /api/interviews"),
    );
    if (
        !check(created, {
            "interview: 201 with an id": (r) =>
                r.status === 201 && typeof safe(r)?.id === "string",
        })
    )
        return;
    const id = safe(created).id;

    // The DB defaults a new run to `pending` and every chat route refuses anything else, so /start belongs
    // in the loop.
    send("POST", "/interviews/{id}/start", null, [200], id);

    let closing = stream("/interviews/{id}/chat/kickoff", null, "opener", id);
    const turns = randInt(TURNS_MIN, TURNS_MAX);
    for (let turn = 1; !closing && turn <= turns; turn++) {
        sleep(rand(THINK_MIN_S, THINK_MAX_S));
        const body = JSON.stringify({
            interview_id: id,
            content: answerText(),
        });
        closing = stream("/interviews/{id}/chat/stream", body, "answer", id);
    }

    const finished = http.post(
        `${API}/interviews/${id}/finish`,
        null,
        params("POST /api/interviews/{id}/finish"),
    );
    check(finished, {
        "finish: 200": (r) => r.status === 200,
        "finish: completed": (r) => safe(r)?.status === "completed",
    });

    const feedback = http.get(
        `${API}/interviews/${id}/feedback`,
        params("GET /api/interviews/{id}/feedback"),
    );
    check(feedback, {
        "feedback: 200": (r) => r.status === 200,
        "feedback: has a review": (r) => (safe(r)?.verdict || "").length > 0,
    });

    transcript(id);
    surface(id);
}

// One turn of SSE from the client's side: the deltas, then `done`. A dropped turn or an error frame
// fails here, in the same report as the timing.
function stream(route, body, label, id) {
    const res = http.post(url(route, id), body, params(`POST /api${route}`));
    const frames = parseSse(res.body);
    const last = frames.length ? frames[frames.length - 1] : null;
    check(res, {
        [`${label}: 200`]: (r) => r.status === 200,
        [`${label}: streamed a message`]: () =>
            frames.some((f) => f.event === "message"),
        [`${label}: ended with done`]: () =>
            last !== null && last.event === "done",
    });
    if (!last || last.event !== "done") return false;
    let ending = {};
    try {
        ending = JSON.parse(last.data || "{}");
    } catch (e) {
        ending = {};
    }
    return ending.closing === true;
}

// A dropped or duplicated turn breaks the alternation, so this is the check that the loop did what it
// reported.
function transcript(id) {
    const res = http.get(
        `${API}/interviews/${id}/messages`,
        params("GET /api/interviews/{id}/messages"),
    );
    if (!check(res, { "transcript: 200": (r) => r.status === 200 })) return;
    const rows = safe(res) || [];
    check(rows, {
        "transcript: one assistant per user turn": (rs) =>
            rs.filter((r) => r.role === "assistant").length ===
            rs.filter((r) => r.role === "user").length + 1,
        "transcript: alternates from the opener": (rs) =>
            rs.every((r, i) => r.role === (i % 2 === 0 ? "assistant" : "user")),
    });
}

// The rest of the signed-in surface (SRE.md decision 6). The two creates are kept off the app's caps by
// deleting what they made in the same iteration.
//
// AI HERE: at hundreds of VUs most creates land on the resume and template caps and are accepted as
// such, so the happy path only runs under the cap. Excluded on purpose: /billing/* (money rails with
// real provider side effects), the BYOK writes (they decide who pays for the turns), voice TTS/STT and
// PDF extract (the doc excludes both, and PDF import OOMs the pod).
const READS = [
    "/interviews",
    "/stats/dimensions",
    "/stats/analytics",
    "/usage",
    "/templates",
    "/resumes",
    "/settings/llm",
    "/voice",
];

function surface(id) {
    for (const route of READS) send("GET", route, null, [200], id);
    send("GET", "/interviews/{id}/offer", null, [200, 404], id);

    send("POST", "/interviews/{id}/archive", null, [200], id);
    send("DELETE", "/interviews/{id}/archive", null, [200], id);

    const resume = send(
        "POST",
        "/resumes",
        { name: "sweep resume", text: answerText() },
        [201, 409],
        id,
    );
    if (resume.status === 201)
        send("DELETE", "/resumes/{id}", null, [204], safe(resume).id);

    const template = send(
        "POST",
        "/templates",
        { ...randomInterview(), name: "sweep template" },
        [201, 409],
        id,
    );
    if (template.status === 201)
        send("DELETE", "/templates/{id}", null, [204], safe(template).id);

    send(
        "POST",
        "/client-errors",
        {
            message: "sweep: synthetic browser beacon",
            route: "/sweep",
            component_stack: "",
        },
        [204],
        id,
    );
}

// One status check per request, over the set of statuses the app documents for that route: a 5xx still
// fails while a cap rejection does not read as an error. The tag keeps the {id} token so one route stays
// one tag across every virtual user.
function send(method, route, body, expected, id) {
    const name = `${method} /api${route}`;
    const res = http.request(
        method,
        url(route, id),
        body === null ? null : JSON.stringify(body),
        {
            ...params(name),
            responseCallback: http.expectedStatuses(...expected),
        },
    );
    check(res, {
        [`${name}: ${expected.join(" or ")}`]: (r) =>
            expected.includes(r.status),
    });
    return res;
}

function url(route, id) {
    return `${API}${route.replace("{id}", id)}`;
}

function params(name, tags) {
    const headers = { "Content-Type": "application/json" };
    if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`;
    if (HOST_HEADER) headers.Host = HOST_HEADER;
    return { headers, tags: { name, ...(tags || {}) } };
}

function parseSse(body) {
    const frames = [];
    for (const block of String(body || "").split("\n\n")) {
        let event = "";
        let data = "";
        for (const line of block.split("\n")) {
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (event) frames.push({ event, data });
    }
    return frames;
}

function safe(res) {
    try {
        return res.json();
    } catch (e) {
        return null;
    }
}

function num(key, fallback) {
    return Number(cfg(key, fallback));
}

function randInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}

function rand(min, max) {
    return Math.random() * (max - min) + min;
}

const ROLES = [
    "Backend Engineer",
    "Frontend Engineer",
    "Platform Engineer",
    "Data Engineer",
    "Full Stack Engineer",
];
const TOPICS = [
    "distributed systems",
    "API design",
    "caching and invalidation",
    "streaming pipelines",
    "cost-aware architecture",
    "observability",
];
const LEVELS = [
    "any",
    "intern",
    "junior",
    "mid",
    "senior",
    "staff",
    "principal",
];
const SIZES = ["any", "1-10", "11-50", "51-200", "201-1k", "1k-5k", "5k+"];
const CYNICISM = ["kind", "neutral", "skeptical"];

function pick(list) {
    return list[randInt(0, list.length - 1)];
}

function randomInterview() {
    return {
        role: pick(ROLES),
        topic: pick(TOPICS),
        level: pick(LEVELS),
        company_size: pick(SIZES),
        cynicism: pick(CYNICISM),
        brutally_honest: Math.random() < 0.3,
        human_mode: Math.random() < 0.8,
        hardcore: Math.random() < 0.5,
        mix_chitchat: randInt(0, 2),
        mix_behavioural: randInt(0, 2),
        mix_technical: randInt(1, 4),
    };
}

// Randomized answers, bounded under the app's per-turn cap so a real run never 422s.
function answerText() {
    const target = randInt(ANSWER_MIN_CHARS, ANSWER_MAX_CHARS);
    let text = "";
    while (text.length < target) {
        text += (text ? " " : "") + pick(SENTENCES);
    }
    return text.slice(0, target);
}

const SENTENCES = [
    "At my last job we ran a queue in front of the write path because the burst was spiky and the database could not absorb it.",
    "The first thing I did was measure, and the measurement said the bottleneck was connection setup rather than the query itself.",
    "We split the hot table by tenant so the index stayed in cache and the p99 dropped by about half.",
    "I picked a cache with a short TTL because correctness mattered more than the hit rate on that endpoint.",
    "The retry budget was the part that bit us, so we added jitter and capped attempts at three with a dead letter path.",
    "For the migration I rehearsed it on a copy, ran it in batches, and kept the old column until the reads moved over.",
    "The incident came from a config drift rather than the code, and the fix was a startup assertion plus an alert on the drift.",
    "I would rather have one source of truth for the number, even if it costs a round trip, than two that disagree.",
    "On the frontend we stopped re-rendering the list on every keystroke, which cut the main thread work by more than half.",
    "The team agreed on an SLO of one second at p95 and we treated anything that broke it as a bug rather than a hardware problem.",
    "Load shedding was the honest answer for us: reject early with a clear status instead of queueing until everything times out.",
    "I documented the tradeoff because the next person would otherwise re-add the thing we removed for a reason.",
];
