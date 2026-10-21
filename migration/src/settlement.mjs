import { Interface, keccak256 } from "ethers";
import { address } from "./core.mjs";
const uint = (v) => {
  if (typeof v !== "string" || !/^\d+$/.test(v))
    throw Error("Settlement amounts must be unsigned integer strings");
  return BigInt(v);
};
const tx = (v) => {
  if (!/^0x[0-9a-f]{64}$/i.test(v))
    throw Error("Invalid settlement transaction reference");
};
export function reconcileSettlement(s, eligible) {
  if (
    s.ownerReviewed !== true ||
    !s.reviewReference ||
    !s.saleTransactions?.length ||
    !s.acquisitionTransactions?.length
  )
    throw Error("Owner-reviewed sale and acquisition records required");
  address(s.milordToken);
  address(s.distributionSafe);
  if (
    !/^0x[0-9a-f]{64}$/i.test(s.milordCodeHash) ||
    !Number.isSafeInteger(s.milordDecimals) ||
    s.milordDecimals < 0 ||
    s.milordDecimals > 36
  )
    throw Error("Reviewed MILORD contract and decimals required");
  if (!s.proceedsAsset) throw Error("Proceeds accounting asset required");
  for (const r of [...s.saleTransactions, ...s.acquisitionTransactions])
    tx(r.tx);
  if (
    new Set(s.saleTransactions.map((r) => r.tx.toLowerCase())).size !==
      s.saleTransactions.length ||
    new Set(s.acquisitionTransactions.map((r) => r.tx.toLowerCase())).size !==
      s.acquisitionTransactions.length
  )
    throw Error("Duplicate settlement reference");
  const sold = s.saleTransactions.reduce((n, r) => n + uint(r.pmfiUnits), 0n),
    proceeds = s.saleTransactions.reduce(
      (n, r) => n + uint(r.proceedsUnits),
      0n,
    );
  const acquired = s.acquisitionTransactions.reduce(
      (n, r) => n + uint(r.milordUnits),
      0n,
    ),
    spent = s.acquisitionTransactions.reduce(
      (n, r) => n + uint(r.proceedsSpentUnits),
      0n,
    );
  const pmfiRemaining = uint(s.pmfiRemainingUnits),
    proceedsRemaining = uint(s.proceedsRemainingUnits),
    proceedsDeductions = uint(s.deductions.proceedsUnits),
    milordDeductions = uint(s.deductions.milordUnits),
    net = uint(s.netMilordUnits);
  if (
    sold + pmfiRemaining !== eligible ||
    spent + proceedsDeductions + proceedsRemaining !== proceeds ||
    net + milordDeductions !== acquired
  )
    throw Error("Settlement does not reconcile");
  if ((proceedsDeductions || milordDeductions) && !s.deductions.reason)
    throw Error("Publish deductions explanation");
  return {
    eligiblePmfi: eligible.toString(),
    soldPmfi: sold.toString(),
    pmfiRemaining: pmfiRemaining.toString(),
    proceeds: proceeds.toString(),
    spent: spent.toString(),
    proceedsRemaining: proceedsRemaining.toString(),
    acquiredMilord: acquired.toString(),
    netMilord: net.toString(),
    evidence:
      "Owner-attested amounts and transaction references; not independent trade execution verification",
  };
}
export async function verifyDistributions(provider, manifest, refs) {
  if (
    Number((await provider.getNetwork()).chainId) !== manifest.destinationChain
  )
    throw Error("Distribution RPC chain mismatch");
  const s = manifest.settlement;
  if (keccak256(await provider.getCode(s.milordToken)) !== s.milordCodeHash)
    throw Error("MILORD code mismatch");
  const finalized = await provider.getBlock("finalized");
  if (!finalized) throw Error("Finality information unavailable");
  const iface = new Interface([
      "event Transfer(address indexed from,address indexed to,uint256 value)",
    ]),
    seen = new Set(),
    received = {};
  const events = [];
  for (const ref of refs) {
    tx(ref.tx);
    if (!Number.isSafeInteger(ref.logIndex) || ref.logIndex < 0)
      throw Error("Invalid log index");
    const key = ref.tx.toLowerCase() + ":" + ref.logIndex;
    if (seen.has(key)) throw Error("Duplicate distribution event");
    seen.add(key);
    const r = await provider.getTransactionReceipt(ref.tx);
    if (
      !r ||
      r.status !== 1 ||
      r.blockNumber > finalized.number ||
      (await provider.getBlock(r.blockNumber))?.hash !== r.blockHash
    )
      throw Error("Distribution not finalized or canonical");
    const log = r.logs.find(
      (l) =>
        l.index === ref.logIndex &&
        address(l.address) === address(s.milordToken),
    );
    if (!log) throw Error("Distribution event not found");
    const parsed = iface.parseLog(log);
    if (
      address(parsed.args.from) !== address(s.distributionSafe) ||
      (await provider.getBlock(r.blockNumber)).timestamp < manifest.createdAt
    )
      throw Error("Wrong distribution sender or pre-allocation event");
    const recipient = address(parsed.args.to);
    received[recipient] = (received[recipient] || 0n) + parsed.args.value;
    events.push({
      tx: ref.tx,
      logIndex: ref.logIndex,
      blockHash: r.blockHash,
      recipient,
      amount: parsed.args.value.toString(),
    });
  }
  const required = {};
  for (const r of manifest.rows)
    required[r.recipient] = (required[r.recipient] || 0n) + BigInt(r.amount);
  for (const [recipient, amount] of Object.entries(received))
    if (required[recipient] === undefined || amount > required[recipient])
      throw Error("Unmatched or excessive distribution");
  const sent = Object.fromEntries(
    Object.entries(required).map(([recipient, amount]) => [
      recipient,
      (received[recipient] || 0n) === amount,
    ]),
  );
  return {
    finalizedBlock: finalized.number,
    verifiedAt: new Date().toISOString(),
    events,
    sent,
    distributedUnits: Object.values(received)
      .reduce((n, v) => n + v, 0n)
      .toString(),
  };
}
