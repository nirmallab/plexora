// Network stubs: anything the video must not really do (spend AI credits, reach
// a real remote host, show a real account). Answer it here with plausible,
// clearly-demo values. Loaded by record.mjs before the page; edit per video.
export default async function stubs(page, cfg) {
  const json = (route, body) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

  // Plexora AI gateway: no credits spent, no real run started.
  await page.route(/\/ai\/v1\/balance/, (r) => json(r, {
    available_credits: 2480,
    estimates: { gating: { units: 8, unit: "marker", credits: 96, affordable: true },
                 qc: { units: 40, unit: "channel", credits: 140, affordable: true } },
  }));
  await page.route(/\/ai\/v1\/runs\?limit/, (r) => json(r, { runs: [] }));
  await page.route(/\/ai\/v1\/runs$/, (r) => json(r, { run_id: "air_demo", job_id: "job_demo" }));
  await page.route(/\/ai\/v1\/runs\/air_demo/, (r) => json(r, { run_id: "air_demo", status: "running" }));
}
