/** eth_getLogs over [fromBlock, toBlock] in windows of `step` blocks; halves the window when the RPC refuses a range
 *  (block-range or result-size limits) down to `minStep`. query(from, to) returns an array of logs. */
export async function chunkedLogs(query, fromBlock, toBlock, { step = 10_000, minStep = 100 } = {}) {
  const out = [];
  let from = fromBlock;
  let size = step;
  while (from <= toBlock) {
    const to = Math.min(toBlock, from + size - 1);
    try {
      out.push(...(await query(from, to)));
      from = to + 1;
    } catch (e) {
      const m = String(e?.shortMessage ?? e?.message ?? e);
      if (size > minStep && /range|limit|too many|exceed|10000|response size/i.test(m)) { size = Math.max(minStep, Math.floor(size / 2)); continue; }
      throw e;
    }
  }
  return out;
}
