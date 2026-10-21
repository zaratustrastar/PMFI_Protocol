import { randomBytes } from "node:crypto";
import { verifyMessage } from "ethers";
import { address, fingerprint, windowStatus } from "./core.mjs";
export function health(store, c) {
  return (
    store?.get("healthy") === "true" &&
    Date.now() - Number(store.get("lastSync") || 0) <
      c.maxIndexAgeSeconds * 1000
  );
}
export function approved(c, approval) {
  return (
    c.mode === "local" ||
    (c.mode === "live" &&
      approval?.configHash === fingerprint(c) &&
      approval?.ownerApproved === true &&
      typeof approval?.reference === "string" &&
      approval.reference.length > 0)
  );
}
export function publicState(store, c, approval) {
  const healthy = c.mode !== "disabled" && health(store, c),
    totals = healthy ? store.totals() : null;
  return {
    mode: c.mode,
    chainId: c.chainId,
    recipientChainId: c.recipientChainId,
    token: c.token,
    safe: c.safe,
    decimals: c.decimals,
    start: c.start,
    end: c.end,
    policyUrl: c.policyUrl,
    window: windowStatus(c),
    healthy,
    enabled: healthy && approved(c, approval) && windowStatus(c) === "Open",
    total: healthy
      ? store
          .deposits()
          .filter(
            (e) =>
              !["pending", "orphaned"].includes(e.status) &&
              e.from !== address(c.safe),
          )
          .reduce((sum, e) => sum + BigInt(e.amount), 0n)
          .toString()
      : null,
    totalEligible: totals
      ? Object.values(totals)
          .reduce((s, v) => s + BigInt(v), 0n)
          .toString()
      : null,
    participants: totals ? Object.keys(totals).length : null,
    asOf: store?.tip()?.number ?? null,
    confirmations: c.confirmations,
  };
}
export function issueChallenge(store, c, wallet, recipient) {
  if (c.bindingFrozen || store.get("allocation"))
    throw Error("Recipient registry frozen");
  wallet = address(wallet);
  recipient = address(recipient);
  if (/^0x0{40}$/.test(wallet) || /^0x0{40}$/.test(recipient))
    throw Error("Zero address not allowed");
  const nonce = randomBytes(32).toString("hex"),
    expires = Date.now() + 300000;
  const message = [
    `PMFI migration recipient binding`,
    `Origin: ${c.origin}`,
    `Migration: ${store.get("configHash")}`,
    `Source chain: ${c.chainId}`,
    `Destination chain: ${c.recipientChainId}`,
    `Depositor: ${wallet}`,
    `Recipient: ${recipient}`,
    `Nonce: ${nonce}`,
    `Expires: ${new Date(expires).toISOString()}`,
    `I confirm control of the recipient address and authorize future MILORD allocation to it. This signature does not transfer tokens.`,
  ].join("\n");
  store.db
    .prepare("DELETE FROM nonces WHERE expires<?")
    .run(Date.now() - 86400000);
  store.db
    .prepare("INSERT INTO nonces VALUES (?,?,?,?,0)")
    .run(nonce, wallet, message, expires);
  return { nonce, message, expires };
}
export async function bind(
  store,
  c,
  chain,
  { nonce, signature, recipientSignature },
) {
  if (c.bindingFrozen || store.get("allocation"))
    throw Error("Recipient registry frozen");
  const q = store.db.prepare("SELECT * FROM nonces WHERE nonce=?").get(nonce);
  if (!q || q.used || q.expires <= Date.now())
    throw Error("Expired or used nonce");
  const recipient = q.message.match(/^Recipient: (0x[0-9a-f]{40})$/m)[1];
  if (!(await chain.signature(q.wallet, q.message, signature)))
    throw Error("Invalid depositor signature");
  // Destination must be controlled by an EOA key. Contract depositors use EIP-1271 on Base,
  // then nominate an EOA; never assume a Base Safe exists on the destination chain.
  if (
    address(verifyMessage(q.message, recipientSignature || signature)) !==
    recipient
  )
    throw Error("Recipient control signature required");
  store.transaction(() => {
    if (c.bindingFrozen || store.get("allocation"))
      throw Error("Recipient registry frozen");
    const used = store.db
      .prepare(
        "UPDATE nonces SET used=1 WHERE nonce=? AND used=0 AND expires>?",
      )
      .run(nonce, Date.now());
    if (used.changes !== 1) throw Error("Nonce already used");
    store.db
      .prepare(
        "INSERT INTO bindings VALUES (?,?,?,?,?) ON CONFLICT(wallet) DO UPDATE SET recipient=excluded.recipient,nonce=excluded.nonce,signature=excluded.signature,verifiedAt=excluded.verifiedAt",
      )
      .run(q.wallet, recipient, nonce, signature, new Date().toISOString());
    store.audit("recipient-bound", {
      wallet: q.wallet,
      recipient,
      nonce,
      message: q.message,
      signature,
      recipientSignature: recipientSignature || signature,
    });
  });
  return { wallet: q.wallet, recipient };
}
