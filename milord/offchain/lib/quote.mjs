// EIP-712 entry quotes for MilordConverter.
// The signer must only sign prices from a manipulation-resistant source (e.g. a v4 pool
// TWAP checked against a second source). There is deliberately NO fallback: if the price
// source is unavailable or stale, no quote is issued and conversion simply waits.
export const QUOTE_TYPES = {
  EntryQuote: [
    { name: "account", type: "address" },
    { name: "weiPerMillion", type: "uint256" },
    { name: "issuedAt", type: "uint64" },
    { name: "expiresAt", type: "uint64" },
    { name: "nonce", type: "bytes32" },
  ],
};

export function domain(chainId, converter) {
  return { name: "MilordConverter", version: "1", chainId, verifyingContract: converter };
}

/**
 * @param priceSource async () => ({ weiPerMillion: bigint, observedAt: number })
 */
export async function issueQuote({ signer, chainId, converter, account, priceSource, now, lifetime = 300, maxPriceAge = 120, randomNonce }) {
  const p = await priceSource();
  if (!p || !p.weiPerMillion || p.weiPerMillion <= 0n) throw new Error("price unavailable: no quote issued");
  if (now - p.observedAt > maxPriceAge) throw new Error("price stale: no quote issued");
  const quote = {
    account,
    weiPerMillion: p.weiPerMillion,
    issuedAt: BigInt(now),
    expiresAt: BigInt(now + lifetime),
    nonce: randomNonce,
  };
  const signature = await signer.signTypedData(domain(chainId, converter), QUOTE_TYPES, quote);
  return { quote, signature };
}
