// Shared runtime for the MILORD services: retries with backoff, alerts, a health/metrics endpoint,
// a JSON state file and a fixed-interval loop that never dies on one failed tick.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Retry `fn` with exponential backoff. Errors marked `permanent` (or matched by `isPermanent`) are not retried. */
export async function retry(fn, { attempts = 5, baseMs = 1000, maxMs = 30_000, isPermanent = () => false, onRetry = () => {}, sleepFn = sleep } = {}) {
  let last;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn(i);
    } catch (e) {
      last = e;
      if (e?.permanent || isPermanent(e) || i === attempts - 1) break;
      const wait = Math.min(maxMs, baseMs * 2 ** i);
      onRetry(e, i + 1, wait);
      await sleepFn(wait);
    }
  }
  throw last;
}

/** Revert reasons that will not change by retrying the same call now (expected states, not outages). */
export function isExpectedRevert(e) {
  const m = String(e?.shortMessage ?? e?.message ?? e);
  return /IsPaused|BelowThreshold|TooSoon|NotClaimable|AlreadySettled|NothingToClaim|OutOfBounds|TooEarly|SpotDeviatesFromTwap|ReferencesDisagree|StalePrice|NothingDue/.test(m);
}

/**
 * Alerts go to stdout (JSON lines) and, when ALERT_WEBHOOK_URL is set, are POSTed as {text, level, service, ...}.
 * Same-key alerts are throttled to one per `throttleMs` so a long outage does not flood the channel.
 */
export function createAlerter({ service, webhook = process.env.ALERT_WEBHOOK_URL, throttleMs = 15 * 60_000, fetchFn = globalThis.fetch, now = Date.now } = {}) {
  const lastSent = new Map();
  const sent = [];
  async function alert(level, key, text, extra = {}) {
    const t = now();
    const prev = lastSent.get(key);
    if (prev !== undefined && t - prev < throttleMs && level !== "resolved") return false;
    lastSent.set(key, t);
    const body = { service, level, key, text, at: new Date(t).toISOString(), ...extra };
    sent.push(body);
    console.log(JSON.stringify({ alert: body }));
    if (webhook && fetchFn) {
      try {
        await fetchFn(webhook, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      } catch (e) {
        console.error(JSON.stringify({ alertDeliveryFailed: String(e?.message ?? e) }));
      }
    }
    return true;
  }
  return { alert, sent, clear: (key) => lastSent.delete(key) };
}

/** Small JSON state file (written atomically). */
export function stateFile(file, initial = {}) {
  const read = () => {
    try {
      return JSON.parse(fs.readFileSync(file, "utf8"));
    } catch {
      return structuredClone(initial);
    }
  };
  const write = (s) => {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(s, (_, v) => (typeof v === "bigint" ? v.toString() : v), 2));
    fs.renameSync(tmp, file);
  };
  return { read, write };
}

/** /healthz (200 while the last successful tick is fresher than `staleMs`, else 503) and /metrics (Prometheus text). */
export function startHealthServer({ port, metrics, staleMs }) {
  const server = http.createServer((req, res) => {
    const age = Date.now() - (metrics.lastSuccessMs ?? 0);
    if (req.url === "/healthz") {
      const ok = metrics.lastSuccessMs && age < staleMs;
      res.writeHead(ok ? 200 : 503, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: !!ok, lastSuccessAgeMs: age, ...metrics.summary() }));
    } else if (req.url === "/metrics") {
      res.writeHead(200, { "content-type": "text/plain; version=0.0.4" });
      res.end(metrics.prometheus());
    } else {
      res.writeHead(404).end();
    }
  });
  server.listen(port);
  return server;
}

export function createMetrics(service) {
  const counters = new Map();
  const gauges = new Map();
  const m = {
    lastSuccessMs: 0,
    inc(name, by = 1) {
      counters.set(name, (counters.get(name) ?? 0) + by);
    },
    set(name, v) {
      gauges.set(name, Number(v));
    },
    summary() {
      return { counters: Object.fromEntries(counters), gauges: Object.fromEntries(gauges) };
    },
    prometheus() {
      const lines = [];
      for (const [k, v] of counters) lines.push(`milord_${service}_${k}_total ${v}`);
      for (const [k, v] of gauges) lines.push(`milord_${service}_${k} ${v}`);
      lines.push(`milord_${service}_last_success_seconds ${Math.floor(m.lastSuccessMs / 1000)}`);
      return lines.join("\n") + "\n";
    },
  };
  return m;
}

/**
 * Run `tick` every `intervalMs`. A failed tick is counted and alerted (after `failAlertAfter` consecutive
 * failures) and the loop continues. When ticks start succeeding again a "resolved" alert is sent.
 */
export async function runLoop({ tick, intervalMs, alerter, metrics, failAlertAfter = 3, stopSignal = { stop: false }, sleepFn = sleep, tickTimeoutMs = 10 * 60_000 }) {
  let failures = 0;
  while (!stopSignal.stop) {
    try {
      // Watchdog: a tick that hangs (dropped transaction, stuck RPC) fails like any other, so it is alerted and the
      // next tick runs. The hung promise is abandoned; on-chain steps are idempotent, so the next tick re-checks.
      await withTimeout(tick(), tickTimeoutMs, "TickTimeout");
      metrics.lastSuccessMs = Date.now();
      metrics.inc("ticks");
      if (failures >= failAlertAfter) await alerter.alert("resolved", "tick", `recovered after ${failures} failed ticks`);
      failures = 0;
    } catch (e) {
      failures++;
      metrics.inc("tick_failures");
      console.error(JSON.stringify({ tickFailed: String(e?.shortMessage ?? e?.message ?? e), failures }));
      if (failures >= failAlertAfter) await alerter.alert("critical", "tick", `${failures} consecutive failed ticks: ${String(e?.shortMessage ?? e?.message ?? e)}`);
    }
    if (!stopSignal.stop) await sleepFn(intervalMs);
  }
}

/** Rejects with `${label}` after `ms`; the underlying work is not cancelled. */
export function withTimeout(promise, ms, label = "Timeout") {
  let t;
  return Promise.race([
    promise,
    new Promise((_, reject) => { t = setTimeout(() => reject(new Error(`${label} after ${ms} ms`)), ms); }),
  ]).finally(() => clearTimeout(t));
}

/**
 * Send and wait for a mined receipt, bounded. A transaction not mined within `timeoutMs` throws TxTimeout (alerted by
 * the caller's step wrapper); the nonce manager is reset so the next tick re-reads the pending nonce instead of
 * stacking behind it. A mined but reverted transaction throws TxReverted.
 */
export async function sendAndWait(txp, { timeoutMs = 3 * 60_000, signer } = {}) {
  const tx = await txp;
  if (!tx?.wait) return tx; // fakes in tests
  try {
    const r = await withTimeout(tx.wait(), timeoutMs, `TxTimeout ${tx.hash}`);
    if (r && r.status === 0) throw new Error(`TxReverted ${tx.hash}`);
    return r;
  } catch (e) {
    signer?.reset?.();
    throw e;
  }
}

/**
 * eth_getLogs in bounded block ranges (public RPCs cap the range). `query(from, to)` returns the logs of one range.
 * Halves the range on a range/size error, down to `minStep`.
 */
export { chunkedLogs } from "../../lib/logs.mjs";

export function requireEnv(name) {
  const v = process.env[name];
  if (!v) throw new Error(`missing env ${name}`);
  return v;
}
