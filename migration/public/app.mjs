import {
  BrowserProvider,
  Contract,
  formatUnits,
  formatEther,
  toUtf8Bytes,
  hexlify,
} from "ethers";
import { transferRequest, errorText } from "./wallet.mjs";
const $ = (id) => document.getElementById(id),
  text = (id, v) => ($(id).textContent = v);
let state,
  raw,
  account,
  balance = 0n,
  binding,
  review,
  challenge,
  busy = false,
  epoch = 0;
const abi = [
  "function balanceOf(address) view returns(uint256)",
  "function decimals() view returns(uint8)",
];
async function api(path, body) {
  const r = await fetch("/api/migration/" + path, {
    cache: "no-store",
    ...(body
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {}),
  });
  const d = await r.json();
  if (!r.ok) throw Error(d.error || "Migration service unavailable");
  return d;
}
function message(v) {
  text("feedback", v);
}
function invalidate() {
  epoch++;
  review = null;
  challenge = null;
  binding = null;
  balance = 0n;
  $("review").close();
  $("deposit").disabled = true;
}
async function refresh() {
  try {
    state = await api("state");
    text(
      "total",
      state.total === null
        ? "Unavailable"
        : formatUnits(state.total, state.decimals),
    );
    text("participants", state.participants ?? "Unavailable");
    text("window", state.window);
    text("safe", state.safe || "Awaiting verified address");
    text("token", state.token || "Awaiting verified token");
    text(
      "gate",
      state.enabled
        ? (state.mode === "local" ? "LOCAL TEST ONLY. " : "") +
            "Deposits are open. Verify your payout address and review before sending."
        : state.window === "Closed"
          ? "The migration window has closed. New deposits are not eligible."
          : "Deposits are disabled pending configuration, approval, or verified indexer availability.",
    );
    if (state.policyUrl) {
      $("policy").href = state.policyUrl;
      $("policy").hidden = false;
      $("policy-pending").hidden = true;
    }
    if (account) await walletData();
  } catch (e) {
    if (state) state.enabled = false;
    text("total", "Unavailable");
    text("participants", "Unavailable");
    text("gate", "Migration data is unavailable. Deposits are disabled.");
    $("deposit").disabled = true;
    message(errorText(e));
  }
}
async function walletData() {
  const current = epoch,
    wallet = account;
  const h = await api("history?wallet=" + wallet);
  if (current !== epoch) return;
  binding = h.binding;
  text(
    "your-total",
    state.decimals === null
      ? "Unavailable"
      : formatUnits(h.total, state.decimals),
  );
  text(
    "allocation",
    h.allocation === null
      ? "Pending calculation"
      : h.allocation + " exact MILORD units",
  );
  text("distribution", h.distribution);
  text("history-status", h.healthy ? "VERIFIED LEDGER" : "DATA NOT CURRENT");
  $("recipient-section").hidden = !state.token;
  if (document.activeElement !== $("recipient"))
    $("recipient").value = binding?.recipient || wallet;
  text(
    "binding-status",
    binding
      ? "Verified recipient: " + binding.recipient
      : "Confirm your payout address before depositing.",
  );
  $("history-rows").replaceChildren();
  for (const e of h.rows) {
    const tr = document.createElement("tr");
    for (const value of [
      new Date(e.timestamp * 1000).toISOString().replace("T", " ").slice(0, 19),
      formatUnits(e.amount, state.decimals),
      e.status,
    ]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    const td = document.createElement("td"),
      a = document.createElement("a");
    a.href = "https://basescan.org/tx/" + e.tx;
    a.textContent = e.tx.slice(0, 8) + "… ↗";
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    td.append(a);
    tr.append(td);
    $("history-rows").append(tr);
  }
  $("history-table").hidden = !h.rows.length;
  $("history-empty").hidden = !!h.rows.length;
  text(
    "history-empty",
    "No indexed deposits yet. A submitted transaction appears after on-chain verification.",
  );
  const chain = Number(await raw.request({ method: "eth_chainId" }));
  if (current !== epoch) return;
  text(
    "network",
    chain === state.chainId
      ? state.mode === "local"
        ? "Local test chain"
        : "Base · 8453"
      : "Wrong network · " + chain,
  );
  $("switch").hidden = chain === state.chainId;
  if (chain === state.chainId && state.token) {
    const token = new Contract(state.token, abi, new BrowserProvider(raw));
    const [b, d] = await Promise.all([
      token.balanceOf(wallet),
      token.decimals(),
    ]);
    if (current !== epoch) return;
    if (Number(d) !== state.decimals) throw Error("Token decimals mismatch");
    balance = b;
    text("balance", formatUnits(balance, state.decimals) + " PMFI");
  } else {
    balance = 0n;
    text("balance", "Unavailable");
  }
  $("deposit").disabled =
    busy || !state.enabled || !binding || chain !== state.chainId;
}
async function connect() {
  invalidate();
  if (!window.pmfiPrivy)
    throw Error(
      "The existing PMFI wallet bridge must be installed for this origin. Wallet connection is unavailable.",
    );
  const s = await window.pmfiPrivy.connect();
  raw = s.ethereumProvider;
  account = s.address;
  if (!raw?.request || !account) throw Error("Wallet provider unavailable");
  const changed = () => {
    invalidate();
    account = null;
    raw = null;
    text("wallet", "Reconnect to continue");
    text("connect", "Connect wallet");
    text("balance", "Unavailable");
    text("your-total", "—");
    $("recipient-section").hidden = true;
    $("history-table").hidden = true;
    $("history-empty").hidden = false;
    text("history-empty", "Reconnect to view this wallet.");
  };
  raw.on?.("accountsChanged", changed);
  raw.on?.("chainChanged", () => {
    invalidate();
    refresh();
  });
  raw.on?.("disconnect", changed);
  window.addEventListener("pmfiPrivyLoggedOut", changed, { once: true });
  text("wallet", account);
  text("connect", account.slice(0, 6) + "…" + account.slice(-4));
  await refresh();
}
function run(fn) {
  return async (e) => {
    e?.preventDefault();
    try {
      await fn();
    } catch (err) {
      message(errorText(err));
    }
  };
}
$("connect").onclick = run(connect);
$("switch").onclick = run(async () => {
  await raw.request({
    method: "wallet_switchEthereumChain",
    params: [{ chainId: "0x" + state.chainId.toString(16) }],
  });
  await refresh();
});
$("max").onclick = () => {
  if (state?.decimals !== null && account)
    $("amount").value = formatUnits(balance, state.decimals);
};
$("challenge").onclick = run(async () => {
  challenge = await api("challenge", {
    wallet: account,
    recipient: $("recipient").value,
  });
  text("signing-message", challenge.message);
});
$("recipient").oninput = () => {
  challenge = null;
  text("signing-message", "");
};
$("bind").onclick = run(async () => {
  if (!account) throw Error("Connect your depositing wallet first");
  challenge =
    challenge ||
    (await api("challenge", {
      wallet: account,
      recipient: $("recipient").value,
    }));
  const signature = await raw.request({
    method: "personal_sign",
    params: [hexlify(toUtf8Bytes(challenge.message)), account],
  });
  await api("bind", {
    nonce: challenge.nonce,
    signature,
    recipientSignature: $("recipient-signature").value || undefined,
  });
  challenge = null;
  message("Payout address verified.");
  await refresh();
});
$("deposit-form").onsubmit = run(async () => {
  await refresh();
  if (!account || !binding)
    throw Error("Connect and verify your payout address first");
  const version = epoch,
    amount = $("amount").value,
    tx = await transferRequest(raw, state, account, amount, balance);
  const gas = BigInt(
    await raw.request({ method: "eth_estimateGas", params: [tx] }),
  );
  const price = BigInt(await raw.request({ method: "eth_gasPrice" }));
  if (version !== epoch) throw Error("Wallet changed during review");
  text("gas", "≈ " + formatEther(gas * price) + " ETH");
  text(
    "review-gas",
    "Estimated Base gas: " +
      formatEther(gas * price) +
      " ETH. The wallet shows final fees.",
  );
  review = {
    version,
    amount,
    tx,
    account,
    safe: state.safe,
    token: state.token,
    recipient: binding.recipient,
  };
  text(
    "review-copy",
    "Deposit " +
      amount +
      " PMFI on " +
      (state.mode === "local" ? "the local test chain." : "Base."),
  );
  text("review-wallet", account);
  text("review-safe", state.safe);
  text("review-token", state.token);
  text("review-recipient", binding.recipient);
  $("ack").checked = false;
  $("confirm").disabled = true;
  $("review").showModal();
});
$("ack").onchange = () => {
  $("confirm").disabled = !$("ack").checked;
};
$("cancel").onclick = () => {
  $("review").close();
  review = null;
};
$("confirm").onclick = run(async () => {
  if (busy || !review || !$("ack").checked) return;
  const r = review;
  busy = true;
  $("confirm").disabled = true;
  $("deposit").disabled = true;
  try {
    await refresh();
    if (
      r.version !== epoch ||
      r.account !== account ||
      r.safe !== state.safe ||
      r.token !== state.token ||
      r.recipient !== binding?.recipient
    )
      throw Error("Review expired; check wallet and addresses again");
    const tx = await transferRequest(raw, state, account, r.amount, balance);
    await raw.request({ method: "eth_estimateGas", params: [tx] });
    // Check the window again immediately before opening the wallet confirmation.
    const live = await api("state");
    if (!live.enabled || live.safe !== r.safe || live.token !== r.token)
      throw Error("Deposits closed or configuration changed");
    $("review").close();
    message("Confirm the direct PMFI transfer in your wallet.");
    if (r.version !== epoch) throw Error("Wallet changed. Review again.");
    const finalTx = await transferRequest(
      raw,
      live,
      account,
      r.amount,
      balance,
    );
    const hash = await raw.request({
      method: "eth_sendTransaction",
      params: [finalTx],
    });
    if (!/^0x[0-9a-f]{64}$/i.test(hash))
      throw Error("Wallet did not return a valid transaction hash");
    $("transaction").href = "https://basescan.org/tx/" + hash;
    $("transaction").hidden = false;
    message(
      "Submitted. Waiting for a successful receipt and independent ledger verification.",
    );
    const receipt = await new BrowserProvider(raw).waitForTransaction(
      hash,
      1,
      120000,
    );
    if (!receipt || receipt.status !== 1)
      throw Error(
        "Transaction failed. No eligible deposit was recorded for this transfer.",
      );
    message(
      "Transaction confirmed. Eligibility remains pending until the indexer verifies the event and required confirmations.",
    );
    await refresh();
  } catch (e) {
    message(errorText(e));
  } finally {
    busy = false;
    review = null;
    await refresh();
  }
});
setInterval(() => {
  if (!state?.start || !state.end) return;
  const target =
    state.window === "Not started"
      ? Date.parse(state.start)
      : Date.parse(state.end);
  const seconds = Math.max(0, Math.floor((target - Date.now()) / 1000));
  text(
    "countdown",
    state.window === "Closed"
      ? "Cutoff: " + state.end
      : `${state.window === "Not started" ? "Starts in" : "Closes in"} ${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h ${Math.floor((seconds % 3600) / 60)}m · UTC`,
  );
  if (Date.now() >= Date.parse(state.end)) $("deposit").disabled = true;
}, 1000);
setInterval(refresh, 15000);
await refresh();
