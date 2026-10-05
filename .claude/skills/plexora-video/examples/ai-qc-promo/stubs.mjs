// Network stubs: nothing real happens through the AI gateway in the film.
//   - balance, estimate and the run id are demo placeholders (no credits spent);
//   - the card's evidence thumbnails are the real session's stored sheets,
//     read-only from the live install's artifact store.
import fs from "fs";
// the live data root, read only: set PLEXORA_LIVE_ROOT before node record.mjs
const ARTIFACTS = `${process.env.PLEXORA_LIVE_ROOT}/.agent/artifacts/lsp11385`;
export default async function stubs(page, cfg) {
  const json = (route, body) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route(/\/ai\/v1\/balance/, (r) => json(r, {
    available_credits: 2480,
    estimates: { gating: { units: 8, unit: "marker", credits: 96, affordable: true },
                 qc: { units: 40, unit: "channel", credits: 140, affordable: true } },
  }));
  await page.route(/\/ai\/v1\/runs\?limit/, (r) => json(r, { runs: [] }));
  await page.route(/\/ai\/v1\/runs$/, (r) => json(r, { run_id: "air_demo", job_id: "job_demo" }));
  await page.route(/\/ai\/v1\/runs\/air_demo/, (r) => json(r, { run_id: "air_demo", status: "running" }));
  await page.route(/\/agent\/v1\/captures\/art_[0-9a-f]+/, (r) => {
    const id = r.request().url().match(/art_[0-9a-f]+/)[0];
    const file = `${ARTIFACTS}/${id}.png`;
    if (!fs.existsSync(file)) return r.fulfill({ status: 404, body: "" });
    return r.fulfill({ status: 200, contentType: "image/png", body: fs.readFileSync(file) });
  });
}
