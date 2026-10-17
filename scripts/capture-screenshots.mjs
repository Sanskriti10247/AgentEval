#!/usr/bin/env node
// Captures the dashboard screenshots used in the README (docs/images/*.png).
//
// Drives a headless Chromium over the DevTools protocol with Node's built-in fetch and
// WebSocket (Node 22+), so it needs no npm packages. The dashboard keeps its API key in
// sessionStorage, so the script opens the dashboard origin, stores the key there exactly as the
// key screen would, then navigates to each page. No authentication is bypassed.
//
//   make up && make migrate && make web-up
//   export AGENTEVAL_API_KEY=ae_...              # read scope is enough
//   EVALUATIONS=$(./scripts/demo.sh | sed -n 's/^EVALUATIONS=//p')
//   AGENTEVAL_EVALUATIONS=$EVALUATIONS node scripts/capture-screenshots.mjs
//
// Optional: AGENTEVAL_API_URL (http://localhost:8000), AGENTEVAL_DASHBOARD_URL
// (http://localhost:3000), CHROMIUM (chromium), OUT_DIR (docs/images).
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const API = process.env.AGENTEVAL_API_URL ?? "http://localhost:8000";
const DASH = process.env.AGENTEVAL_DASHBOARD_URL ?? "http://localhost:3000";
const KEY = process.env.AGENTEVAL_API_KEY;
const EVALS = (process.env.AGENTEVAL_EVALUATIONS ?? "").split(",").filter(Boolean);
const CHROMIUM = process.env.CHROMIUM ?? "chromium";
const OUT = process.env.OUT_DIR ?? "docs/images";
const PORT = 9333;

if (!KEY || EVALS.length < 2) {
  console.error("set AGENTEVAL_API_KEY and AGENTEVAL_EVALUATIONS=<id>,<id>[,<id>] (see the header)");
  process.exit(2);
}

const api = async (path) => {
  const r = await fetch(`${API}/api/v1${path}`, { headers: { Authorization: `Bearer ${KEY}` } });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// A run of the first evaluation (an agent run, so it has a trajectory).
const { runs } = await api(`/runs?evaluation_id=${EVALS[0]}&limit=5`);
const run = runs.find((r) => r.status === "COMPLETED") ?? runs[0];

const pages = [
  { file: "overview.png", url: `${DASH}/`, ready: "Evaluations" },
  { file: "evaluation.png", url: `${DASH}/evaluations/${EVALS[0]}`, ready: "Failure", height: 1250 },
  { file: "compare.png", url: `${DASH}/evaluations/compare?ids=${EVALS.join(",")}`, ready: "Compare" },
  { file: "run.png", url: `${DASH}/runs/${run.id}`, ready: "Trajectory", height: 1500 },
];

const profile = mkdtempSync(join(tmpdir(), "agenteval-shots-"));
const chrome = spawn(
  CHROMIUM,
  [
    "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, "--window-size=1440,900",
    "about:blank",
  ],
  { stdio: "ignore" },
);
const cleanup = () => {
  chrome.kill();
  rmSync(profile, { recursive: true, force: true });
};
process.on("exit", cleanup);

let target;
for (let i = 0; i < 50 && !target; i++) {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
    target = await res.json();
  } catch {
    await sleep(200);
  }
}
if (!target) throw new Error("could not reach Chromium's debugging port");

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let nextId = 0;
const pending = new Map();
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { res, rej } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
  }
};
const send = (method, params = {}) =>
  new Promise((res, rej) => {
    const id = ++nextId;
    pending.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params }));
  });
const evaluate = async (expression) =>
  (await send("Runtime.evaluate", { expression, returnByValue: true })).result.value;

await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: DASH });
await sleep(1500);
await evaluate(`sessionStorage.setItem("agenteval.apiKey", ${JSON.stringify(KEY)})`);

mkdirSync(OUT, { recursive: true });
for (const p of pages) {
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1440, height: p.height ?? 900, deviceScaleFactor: 1, mobile: false,
  });
  await send("Page.navigate", { url: p.url });
  // Wait until the page text contains the marker and no "Loading" placeholder remains.
  let ok = false;
  for (let i = 0; i < 60 && !ok; i++) {
    await sleep(500);
    ok = await evaluate(
      `document.body.innerText.includes(${JSON.stringify(p.ready)}) && !/Loading/.test(document.body.innerText)`,
    );
  }
  if (!ok) console.error(`warning: ${p.file}: marker "${p.ready}" not seen; capturing anyway`);
  await sleep(1200); // charts animate in
  const { data } = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(join(OUT, p.file), Buffer.from(data, "base64"));
  console.log(`wrote ${join(OUT, p.file)}`);
}
ws.close();
cleanup();
process.exit(0);
