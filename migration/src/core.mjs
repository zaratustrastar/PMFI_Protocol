import { createHash } from "node:crypto";
import { getAddress, ZeroAddress } from "ethers";
export const address = (a) => getAddress(a).toLowerCase();
export const fingerprint = (c) =>
  createHash("sha256").update(JSON.stringify(c)).digest("hex");
export function validateConfig(c) {
  if (!["disabled", "local", "live"].includes(c.mode))
    throw Error("Invalid mode");
  if (c.mode === "disabled") return c;
  if (
    c.chainId !== (c.mode === "local" ? 31337 : 8453) ||
    c.recipientChainId !== 4663
  )
    throw Error("Invalid chain");
  for (const k of ["token", "safe"])
    if (address(c[k]) === ZeroAddress) throw Error("Zero address");
  if (address(c.token) === address(c.safe))
    throw Error("Token cannot be receiving Safe");
  for (const k of ["start", "end"])
    if (
      typeof c[k] !== "string" ||
      !c[k].endsWith("Z") ||
      !Number.isFinite(Date.parse(c[k]))
    )
      throw Error("Window must be UTC");
  if (Date.parse(c.start) >= Date.parse(c.end)) throw Error("Invalid window");
  for (const k of [
    "startBlock",
    "confirmations",
    "decimals",
    "threshold",
    "maxIndexAgeSeconds",
  ])
    if (!Number.isSafeInteger(c[k])) throw Error("Invalid " + k);
  if (
    c.startBlock < 1 ||
    c.confirmations < 1 ||
    c.decimals < 0 ||
    c.decimals > 36 ||
    c.maxIndexAgeSeconds < 1 ||
    c.maxIndexAgeSeconds > 600
  )
    throw Error("Invalid numeric configuration");
  const owners = c.owners.map(address);
  if (
    owners.includes(ZeroAddress) ||
    new Set(owners).size !== owners.length ||
    // Custody configuration is owner-approved. A deployed 1-of-1 Safe is
    // valid, while larger teams may choose a higher threshold.
    c.threshold < 1 ||
    c.threshold > owners.length
  )
    throw Error("Invalid Safe owners/threshold");
  for (const k of ["tokenCodeHash", "safeCodeHash", "safeSingletonCodeHash"])
    if (!/^0x[0-9a-f]{64}$/i.test(c[k])) throw Error("Missing code hash " + k);
  if (c.tokenAccountingReview !== "net-transfer-events-no-rebase")
    throw Error("Token accounting review required");
  if (!c.policyUrl || new URL(c.policyUrl).protocol !== "https:")
    throw Error("Published policy required");
  const origin = new URL(c.origin);
  if (
    origin.origin !== c.origin ||
    (c.mode === "live" && origin.protocol !== "https:")
  )
    throw Error("Invalid origin");
  return c;
}
export function windowStatus(c, now = Date.now()) {
  if (c.mode === "disabled" || !c.start || !c.end) return "Not scheduled";
  return now < Date.parse(c.start)
    ? "Not started"
    : now >= Date.parse(c.end)
      ? "Closed"
      : "Open";
}
export function eligibility(c, e, head) {
  if (e.orphaned) return "orphaned";
  if (head - e.blockNumber + 1 < c.confirmations) return "pending";
  if (
    e.from === ZeroAddress ||
    e.from === address(c.safe) ||
    BigInt(e.amount) === 0n
  )
    return "review";
  if (e.timestamp * 1000 < Date.parse(c.start)) return "early-review";
  if (e.timestamp * 1000 >= Date.parse(c.end)) return "late-review";
  return "eligible";
}
export function allocate(totals, pool) {
  if (!/^[0-9]+$/.test(String(pool)))
    throw Error("Pool must be exact integer units");
  pool = BigInt(pool);
  const rows = Object.entries(totals)
    .map(([wallet, amount]) => ({
      wallet: address(wallet),
      weight: BigInt(amount),
    }))
    .sort((a, b) => a.wallet.localeCompare(b.wallet));
  if (
    new Set(rows.map((r) => r.wallet)).size !== rows.length ||
    rows.some((r) => r.weight <= 0n)
  )
    throw Error("Invalid participant totals");
  const total = rows.reduce((s, r) => s + r.weight, 0n);
  if (!total) throw Error("No eligible deposits");
  for (const r of rows) {
    r.amount = (pool * r.weight) / total;
    r.remainder = (pool * r.weight) % total;
  }
  let dust = pool - rows.reduce((s, r) => s + r.amount, 0n);
  for (const r of [...rows].sort((a, b) =>
    a.remainder === b.remainder
      ? a.wallet.localeCompare(b.wallet)
      : a.remainder > b.remainder
        ? -1
        : 1,
  ))
    if (dust-- > 0n) r.amount++;
  return rows.map(({ wallet, amount }) => ({
    wallet,
    amount: amount.toString(),
  }));
}
