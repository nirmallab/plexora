// Jobs on the website, end to end in Chrome: long work runs in the website's
// own Python between requests, its progress is answered by the page while
// the worker is busy with it, it can be cancelled while it runs, and what it
// computes is what the desktop computes. Projects are made inside the worker
// from the test suite's own fixture modules (the seeds and sizes come from
// --spec, which tests/test_browser_jobs.py also gives CPython):
//
//   apply     apply_gate_to_dataset over a cohort (POST /agent/v1/capabilities/
//             apply_gate_to_dataset), followed through GET /agent/v1/jobs/<id>;
//   qc        the QC panel's blur check (POST /plugins/qc/blur/run), followed
//             through the panel's own GET /plugins/qc/jobs/<id>;
//   classify  the Single Cell Classifier (classifier_run, auto_pick),
//             followed through GET /plugins/gating/jobs/<id>;
//   cancel    the blur check again (force), cancelled from the panel's route
//             while it runs: the page flags it in shared memory and the job
//             stops at its next check.
//
// While each job runs the driver polls its route every 50 ms and records who
// answered (`answered_by: "page"`: the mirror of the job's record the worker
// posts as it writes it) and the progress it said.
//
//   PLEXORA_BENCH_HOME=<bundle home> node tests/browser/jobs.mjs --spec JSON [--shots DIR] [--headed]
//
// Prints JSON: {checks, results: {apply, qc, classify}, polls, errors}.
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, startServer } from "./lib.mjs";
import { keepInBrowser, py, shipFixtures } from "./ai_lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const spec = JSON.parse(opt("--spec", "{}"));
const shots = opt("--shots", null);
const cohort = spec.cohort || { shifts: [0.0, 0.0, 0.4, 0.2, 0.1, 0.3], grid: 16, size: 384 };
const qc = spec.qc || { size: 1024, grid: 40, artifacts: ["blur_local"], channel: "DNA_2" };
const gate = spec.gate || { marker: "CD3", low: 500.0 };

const out = { checks: [], results: {}, polls: {}, errors: [] };
const check = (name, ok, detail = {}) => {
    out.checks.push({ name, ok: Boolean(ok), ...detail });
    console.error(`${ok ? "ok  " : "FAIL"} ${name}`);
};

const temp = mkdtempSync(path.join(tmpdir(), "pxai-jobs-"));
const site = path.join(temp, "site");
execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", site],
             { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO, PLEXORA_DATA_PATH: path.join(temp, "data") },
               stdio: ["ignore", "ignore", "inherit"] });
const server = await startServer(["--site", site]);
const browser = await launch({ headed: args.includes("--headed") });
const origin = `http://127.0.0.1:${server.port}`;

const MAKE = `
import json
from tests.autogate_fixtures import make_gating_dataset, make_gating_project
from tests.qc_fixtures import make_qc_project
made = make_gating_dataset("/data", "cohort", shifts=${JSON.stringify(cohort.shifts)}, grid=${cohort.grid},
                           size=${cohort.size})
make_qc_project("/data", "qcsynth", size=${qc.size}, grid=${qc.grid}, artifacts=${JSON.stringify(qc.artifacts)})
json.dumps({"cohort": sorted(made)})
`;

//: Start a job from the page (its fetch, through the facade), then poll its
//: route every 50 ms until it ends: each answer's status, progress and who
//: answered. `cancelAfter`: a progress count at which the panel's Cancel is
//: pressed (its route, from the page).
async function follow(page, { start, poll, cancel = null, cancelAfter = null, limitMs = 300000 }) {
    return page.evaluate(async ({ start, poll, cancel, cancelAfter, limitMs }) => {
        const began = performance.now();
        const started = await (await fetch(start.url, { method: "POST", headers: { "Content-Type": "application/json" },
                                                        body: JSON.stringify(start.body) })).json();
        const id = started.job_id || (started.result && started.result.job_id) || (started.data && started.data.job_id);
        if (!id) return { started, error: "no job id" };
        const seen = [];
        let last = null;
        let cancelled = null;
        while (performance.now() - began < limitMs) {
            const asked = performance.now();
            const answer = await (await fetch(poll.replace("{id}", id))).json();
            const job = answer.job || {};
            const progress = job.progress || {};
            seen.push({ at: Math.round(asked - began), ms: Math.round(performance.now() - asked),
                        by: answer.answered_by || "python", status: job.status || job.state,
                        done: progress.done ?? null, total: progress.total ?? null });
            last = answer;
            if (cancel && cancelled === null && (progress.done || 0) >= cancelAfter && job.status === "running") {
                cancelled = await (await fetch(cancel.replace("{id}", id), { method: "POST",
                    headers: { "Content-Type": "application/json" }, body: "{}" })).json();
            }
            const status = job.status || job.state;
            if (["done", "failed", "cancelled", "interrupted"].includes(status)) break;
            await new Promise((resolve) => setTimeout(resolve, 50));
        }
        return { id, seen, last, cancelled, ms: Math.round(performance.now() - began) };
    }, { start, poll, cancel, cancelAfter, limitMs });
}

const summary = (followed) => {
    const seen = followed.seen || [];
    const running = seen.filter((s) => s.status === "running");
    return { polls: seen.length, byPage: seen.filter((s) => s.by === "page").length,
             runningPolls: running.length, maxPollMs: Math.max(0, ...seen.map((s) => s.ms)),
             progress: [...new Set(running.map((s) => `${s.done}/${s.total}`))].slice(0, 20), ms: followed.ms };
};

try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    page.on("pageerror", (error) => out.errors.push(`pageerror: ${String(error).slice(0, 300)}`));
    page.on("crash", () => out.errors.push("the page crashed"));
    await page.goto(`${origin}/?pydebug`);
    const frame = await appFrame(page);
    await keepInBrowser(frame);
    await page.evaluate(() => window.PlexoraPython.ready);
    await shipFixtures(page);
    const made = await py(page, MAKE);
    check("the cohort and the QC project are made in the website's Python", made.cohort.length === cohort.shifts.length,
          { cohort: made.cohort });
    const shared = await page.evaluate(() => window.crossOriginIsolated && typeof SharedArrayBuffer === "function");

    // apply_gate_to_dataset
    const apply = await follow(page, {
        start: { url: "/agent/v1/capabilities/apply_gate_to_dataset",
                 body: { arguments: { dataset: "cohort", marker: gate.marker, low: gate.low } } },
        poll: "/agent/v1/jobs/{id}" });
    out.polls.apply = summary(apply);
    const applied = apply.last && apply.last.job;
    out.results.apply = applied && applied.result;
    check("apply_gate_to_dataset runs as a job and ends done", applied && applied.status === "done"
          && out.results.apply && out.results.apply.projects.length === cohort.shifts.length,
          { status: applied && applied.status, error: applied && applied.error });
    check("its progress is answered by the page while it runs, image by image",
          out.polls.apply.byPage >= 2 && out.polls.apply.progress.length >= 2, out.polls.apply);

    // The QC panel's blur check
    const blur = await follow(page, {
        start: { url: "/plugins/qc/blur/run", body: { datasource: "qcsynth", channel: qc.channel } },
        poll: "/plugins/qc/jobs/{id}" });
    out.polls.qc = summary(blur);
    const blurred = blur.last && blur.last.job;
    check("the QC panel's blur check runs as a job and ends done", blurred && blurred.status === "done",
          { status: blurred && blurred.status, error: blurred && blurred.error, started: blur.started });
    check("the panel's own job route is answered by the page while it runs", out.polls.qc.byPage >= 1, out.polls.qc);
    out.results.qc = await page.evaluate(async (channel) => (await (await fetch(
        `/plugins/qc/blur?datasource=qcsynth&channel=${channel}`)).json()), qc.channel);

    // The Single Cell Classifier
    const classify = await follow(page, {
        start: { url: "/agent/v1/capabilities/classifier_run",
                 body: { arguments: { project: "cohort_1", marker: gate.marker, auto_pick: true } } },
        poll: "/plugins/gating/jobs/{id}" });
    out.polls.classify = summary(classify);
    const classified = classify.last && classify.last.job;
    out.results.classify = classified && classified.result;
    check("the classifier runs as a job and ends done", classified && classified.status === "done",
          { status: classified && classified.status, error: classified && classified.error, started: classify.started });

    // Cancel, from the panel, while it runs
    const stopped = await follow(page, {
        start: { url: "/plugins/qc/blur/run", body: { datasource: "qcsynth", channels: ["DNA_1", "CD3", "CD8", "DNA_2", "CD20"],
                                                       force: true } },
        poll: "/plugins/qc/jobs/{id}", cancel: "/plugins/qc/jobs/{id}/cancel", cancelAfter: 0 });
    out.polls.cancel = summary(stopped);
    const ended = stopped.last && stopped.last.job;
    check("a job cancelled from its panel while it runs stops", shared && stopped.cancelled
          && stopped.cancelled.ok && ended && ended.status === "cancelled",
          { shared, cancelled: stopped.cancelled, status: ended && ended.status, error: ended && ended.error });
    if (shots) await page.screenshot({ path: path.join(shots, "jobs.png") });
} catch (error) {
    out.errors.push(String(error && error.stack || error).slice(0, 2000));
} finally {
    await browser.close();
    server.stop();
    rmSync(temp, { recursive: true, force: true });
}
out.ok = out.checks.every((c) => c.ok) && out.errors.length === 0;
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
