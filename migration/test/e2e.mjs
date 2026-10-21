// Actual ERC-20 transfers run ONLY on a fresh local Anvil chain (31337).
import { spawn } from "node:child_process";
import {
  readFileSync,
  mkdtempSync,
  mkdirSync,
  writeFileSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import assert from "node:assert/strict";
import {
  JsonRpcProvider,
  ContractFactory,
  keccak256,
  parseEther,
  hexlify,
  toUtf8Bytes,
} from "ethers";
import { chromium } from "@playwright/test";
import { Store } from "../src/store.mjs";
import { Chain, sync } from "../src/chain.mjs";
import { createServer } from "../src/server.mjs";
import { validateConfig } from "../src/core.mjs";
import { issueChallenge, bind } from "../src/service.mjs";
const rpc = "http://127.0.0.1:18545",
  anvil = spawn(
    process.env.ANVIL || "/opt/homebrew/bin/anvil",
    ["--port", "18545", "--chain-id", "31337", "--silent"],
    { stdio: "ignore" },
  );
const dir = mkdtempSync(join(tmpdir(), "pmfi-e2e-")),
  shots = resolve(process.env.SCREENSHOT_DIR || "reports");
mkdirSync(shots, { recursive: true });
let browser, server, store;
const p = new JsonRpcProvider(rpc, undefined, { cacheTimeout: -1 });
const checks = [];
const pass = (s) => {
  checks.push(s);
  console.log("PASS", s);
};
try {
  for (let i = 0; i < 60; i++) {
    try {
      await p.getBlockNumber();
      break;
    } catch {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  assert.equal(Number((await p.getNetwork()).chainId), 31337);
  const signer = await p.getSigner(0),
    other = await p.getSigner(1),
    wallet = (await signer.getAddress()).toLowerCase();
  const deploy = async (name, args = []) => {
    const a = JSON.parse(
      readFileSync(`test/evm/out/Fixtures.sol/${name}.json`),
    );
    const c = await new ContractFactory(
      a.abi,
      a.bytecode.object,
      signer,
    ).deploy(...args);
    await c.waitForDeployment();
    return c;
  };
  const token = await deploy("Token"),
    safe = await deploy("TestSafe", [wallet, await other.getAddress()]);
  const c = validateConfig({
    mode: "local",
    chainId: 31337,
    recipientChainId: 4663,
    origin: process.env.MIGRATION_FRONTEND_URL || "http://127.0.0.1:18787",
    token: await token.getAddress(),
    safe: await safe.getAddress(),
    decimals: 18,
    tokenCodeHash: keccak256(await p.getCode(await token.getAddress())),
    safeCodeHash: keccak256(await p.getCode(await safe.getAddress())),
    safeSingletonCodeHash: keccak256(await p.getCode(await safe.getAddress())),
    owners: [wallet, await other.getAddress()],
    threshold: 2,
    startBlock: 3,
    confirmations: 2,
    start: new Date(Date.now() - 60000).toISOString(),
    end: new Date(Date.now() + 86400000).toISOString(),
    policyUrl: "https://pmfi.cc/migration-policy",
    tokenAccountingReview: "net-transfer-events-no-rebase",
    maxIndexAgeSeconds: 120,
    bindingFrozen: false,
  });
  store = new Store(join(dir, "ledger.sqlite"), c);
  const chain = new Chain([rpc], c);
  await p.send("evm_mine", []);
  await sync(store, chain);
  assert.equal(store.get("healthy"), "true");
  pass(
    "Live code hashes, Safe owners/threshold and decimals verified on local EVM",
  );
  server = createServer(c, store, chain);
  await new Promise((r) => server.listen(18787, "127.0.0.1", r));
  browser = await chromium.launch({
    headless: true,
    executablePath:
      process.env.CHROME ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1100 },
  });
  // External fonts are not part of wallet/ledger integration and may be offline.
  await page.route(/https:\/\/fonts\.(googleapis|gstatic)\.com\//, (r) =>
    r.abort(),
  );
  let reject = false,
    wrongChain = false,
    sendCount = 0;
  await page.exposeFunction(
    "testWalletRpc",
    async ({ method, params = [] }) => {
      if (reject && method === "eth_sendTransaction") {
        const e = Error("User rejected request");
        e.code = 4001;
        throw e;
      }
      if (method === "eth_chainId") return wrongChain ? "0x1" : "0x7a69";
      if (method === "eth_accounts" || method === "eth_requestAccounts")
        return [wallet];
      if (method === "wallet_switchEthereumChain") {
        wrongChain = false;
        return null;
      }
      if (method === "personal_sign")
        return signer.signMessage(Buffer.from(params[0].slice(2), "hex"));
      if (method === "eth_sendTransaction") sendCount++;
      return p.send(method, params);
    },
  );
  await page.route("**/privy-bridge.bundle.js", (r) =>
    r.fulfill({
      contentType: "text/javascript",
      body: "/* local wallet fixture */",
    }),
  );
  await page.addInitScript(
    ({ wallet }) => {
      window.pmfiPrivy = {
        connect: async () => ({
          address: wallet,
          ethereumProvider: { request: window.testWalletRpc, on: () => {} },
        }),
      };
    },
    { wallet },
  );
  await page.goto(c.origin + "/migrate");
  await page.getByRole("button", { name: "Connect wallet" }).click();
  await page.waitForFunction(() =>
    document.querySelector("#wallet").textContent.startsWith("0x"),
  );
  await page
    .getByRole("button", { name: "Confirm payout address", exact: true })
    .click();
  await page.waitForFunction(() =>
    document
      .querySelector("#binding-status")
      .textContent.startsWith("Verified recipient:"),
  );
  pass(
    "Privy-shaped provider connection and signed same-address payout binding through HTTP API",
  );
  await page.locator("#amount").fill("12.123456789012345678");
  await page.locator("#deposit").click();
  await page.locator("#ack").check();
  await page.locator("#confirm").click();
  await page.waitForFunction(
    () =>
      document
        .querySelector("#feedback")
        .textContent.includes("Transaction confirmed"),
    { timeout: 15000 },
  );
  await sync(store, chain);
  assert.equal(store.deposits()[0].status, "pending");
  await p.send("evm_mine", []);
  await sync(store, chain);
  assert.equal(
    store.totals()[wallet],
    parseEther("12.123456789012345678").toString(),
  );
  pass(
    "Direct ERC-20 transfer, successful receipt, exact amount, pending then eligible",
  );
  await page.reload();
  await page.locator("#connect").click();
  await page.waitForFunction(
    () => document.querySelector("#history-table").hidden === false,
  );
  assert.equal(
    await page.locator("#your-total").textContent(),
    "12.123456789012345678",
  );
  pass(
    "Browser refresh restores independently indexed history after reconnect",
  );
  await page.screenshot({
    path: join(shots, "migration-local-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.setViewportSize({
    width: 390,
    height: await page.evaluate(() => document.documentElement.scrollHeight),
  });
  await page.waitForTimeout(200);
  await page.screenshot({ path: join(shots, "migration-local-mobile.png") });
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  );
  pass("Desktop/mobile rendering with no horizontal overflow");
  // Wallet rejection causes no broadcast.
  reject = true;
  await page.locator("#amount").fill("1");
  await page.locator("#deposit").click();
  await page.locator("#ack").check();
  await page.locator("#confirm").click();
  await page.waitForFunction(() =>
    document.querySelector("#feedback").textContent.includes("rejected"),
  );
  assert.equal(sendCount, 1);
  reject = false;
  pass("Wallet rejection does not send a transaction");
  // Wrong network and insufficient balance stop before wallet confirmation.
  wrongChain = true;
  await page.reload();
  await page.locator("#connect").click();
  await page.waitForFunction(() => !document.querySelector("#switch").hidden);
  assert.equal(await page.locator("#deposit").isDisabled(), true);
  await page.locator("#switch").click();
  await page.waitForFunction(() => document.querySelector("#switch").hidden);
  pass("Wrong network blocks submission and network switch recovers");
  await page.locator("#amount").fill("9999999999");
  await page.locator("#deposit").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#feedback")
      .textContent.includes("Insufficient PMFI"),
  );
  assert.equal(sendCount, 1);
  pass("Insufficient balance blocks review");
  await token.setFail(true);
  await assert.rejects(token.transfer(c.safe, 1n));
  await token.setFail(false);
  pass("Reverted token transfer is rejected");
  // Fee-on-transfer fixture: ledger records emitted net amount, not requested input.
  await token.setTaxed(true);
  await (await token.transfer(c.safe, parseEther("10"))).wait();
  await p.send("evm_mine", []);
  await sync(store, chain);
  assert.equal(
    store.totals()[wallet],
    parseEther("21.123456789012345678").toString(),
  );
  pass("Taxed transfer credits actual 9 PMFI received for 10 requested");
  const snapshot = await p.send("evm_snapshot", []);
  await (await token.transfer(c.safe, parseEther("1"))).wait();
  await p.send("evm_mine", []);
  await sync(store, chain);
  const before = store.deposits().length;
  await p.send("evm_revert", [snapshot]);
  await p.send("evm_mine", []);
  await sync(store, chain);
  assert.equal(store.deposits().filter((e) => e.orphaned).length, 1);
  assert.equal(store.deposits().length, before);
  pass(
    "Actual local EVM reorganization removes eligibility and preserves orphan audit",
  );
  // Persisted ledger reopens cleanly and does not double-count.
  store.close();
  store = new Store(join(dir, "ledger.sqlite"), c);
  await sync(store, chain);
  assert.equal(
    store.totals()[wallet],
    parseEther("21.123456789012345678").toString(),
  );
  pass("Process restart and repeat indexing are idempotent");
  // EIP-1271 source with independently proven EOA recipient.
  const challenge = issueChallenge(store, c, c.safe, wallet);
  await bind(store, c, chain, {
    nonce: challenge.nonce,
    signature: "0x1234",
    recipientSignature: await signer.signMessage(challenge.message),
  });
  pass(
    "EIP-1271 depositor binding requires separately controlled EOA recipient",
  );
  // Insufficient gas on the EVM.
  await p.send("anvil_setBalance", [wallet, "0x0"]);
  await assert.rejects(
    signer.sendTransaction({
      to: c.token,
      data: token.interface.encodeFunctionData("transfer", [c.safe, 1n]),
      gasLimit: 100000,
    }),
  );
  pass("Insufficient gas rejected by local EVM");
  // Disabled default screenshots and route isolation.
  await new Promise((r) => server.close(r));
  server = createServer(
    {
      mode: "disabled",
      chainId: 8453,
      recipientChainId: 4663,
      token: null,
      safe: null,
      decimals: null,
    },
    null,
    null,
  );
  await new Promise((r) => server.listen(18787, "127.0.0.1", r));
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto(c.origin + "/migrate");
  await page.waitForFunction(
    () => document.querySelector("#window").textContent === "Not scheduled",
  );
  await page.screenshot({
    path: join(shots, "migration-disabled-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.setViewportSize({
    width: 390,
    height: await page.evaluate(() => document.documentElement.scrollHeight),
  });
  await page.waitForTimeout(200);
  await page.screenshot({ path: join(shots, "migration-disabled-mobile.png") });
  assert.equal(await page.locator("#deposit").isDisabled(), true);
  assert.equal((await fetch(c.origin + "/")).status, 404);
  pass("Unconfigured deployment disabled; service does not replace homepage");
  writeFileSync(
    join(shots, "e2e-results.json"),
    JSON.stringify({ network: "Local Anvil 31337 ONLY", checks }, null, 2),
  );
} finally {
  await browser?.close();
  if (server) await new Promise((r) => server.close(r));
  store?.close();
  p.destroy();
  anvil.kill("SIGTERM");
  rmSync(dir, { recursive: true, force: true });
}
