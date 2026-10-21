import { isIP } from "node:net";
export function clientAddress(req, trustLoopbackProxy = false) {
  const peer = req.socket.remoteAddress;
  if (
    trustLoopbackProxy &&
    ["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(peer)
  ) {
    // Nginx must OVERWRITE this header from $remote_addr, not forward user input.
    const forwarded = req.headers["x-real-ip"];
    if (typeof forwarded === "string" && isIP(forwarded)) return forwarded;
  }
  return peer || "unknown";
}
export function createLimiter({
  limit = 30,
  windowMs = 60000,
  maxClients = 10000,
} = {}) {
  const rates = new Map();
  return (key, now = Date.now()) => {
    for (const [k, v] of rates) if (now - v.at >= windowMs) rates.delete(k);
    if (!rates.has(key) && rates.size >= maxClients) return false;
    const r = rates.get(key) || { at: now, n: 0 };
    rates.set(key, r);
    return ++r.n <= limit;
  };
}
