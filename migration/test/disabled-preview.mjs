// Verifies the default React preview build cannot sign/send even against a live-enabled API.
import assert from "node:assert/strict";
import { chromium } from "@playwright/test";
const browser = await chromium.launch({
  headless: true,
  executablePath:
    process.env.CHROME ||
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
});
try {
  const page = await browser.newPage();
  let offline = false;
  await page.route(/https:\/\/fonts\.(googleapis|gstatic)\.com\//, (r) =>
    r.abort(),
  );
  const wallet = "0x" + "1".repeat(40),
    token = "0x" + "2".repeat(40);
  await page.route("**/api/migration/**", (r) =>
    offline
      ? r.abort()
      : r.fulfill({
          json: r.request().url().includes("/state")
            ? {
                mode: "live",
                chainId: 8453,
                recipientChainId: 4663,
                token,
                safe: "0x" + "3".repeat(40),
                decimals: 18,
                configHash: "fixture",
                enabled: true,
                healthy: true,
                total: "0",
                participants: 0,
                window: "Open",
                start: null,
                end: null,
                bindingFrozen: false,
              }
            : {
                healthy: true,
                total: "0",
                binding: { recipient: wallet },
                allocation: null,
                allocationDecimals: null,
                distribution: "Not started",
                rows: [],
              },
        }),
  );
  await page.addInitScript(
    ({ wallet }) => {
      window.requests = [];
      window.pmfiPrivy = {
        connect: async () => ({
          address: wallet,
          ethereumProvider: {
            request: async ({ method, params }) => {
              window.requests.push(method);
              if (method === "eth_chainId") return "0x2105";
              if (method === "eth_accounts") return [wallet];
              if (method === "eth_call")
                return (
                  "0x" +
                  BigInt(params[0].data.startsWith("0x313ce567") ? 18 : 100)
                    .toString(16)
                    .padStart(64, "0")
                );
              throw Error("Unexpected wallet request " + method);
            },
          },
        }),
      };
    },
    { wallet },
  );
  await page.goto(
    (process.env.MIGRATION_FRONTEND_URL || "http://127.0.0.1:5173") +
      "/migrate",
  );
  await page.locator("#connect").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#binding-status")
      ?.textContent.startsWith("Verified recipient:"),
  );
  assert.equal(await page.locator("#deposit").isDisabled(), true);
  assert.equal(await page.locator("#bind").isDisabled(), true);
  assert.match(await page.locator("#gate").textContent(), /Disabled preview/);
  assert.equal(
    await page.evaluate(() =>
      window.requests.some((x) =>
        ["personal_sign", "eth_sendTransaction"].includes(x),
      ),
    ),
    false,
  );
  offline = true;
  await page.reload();
  await page.waitForFunction(
    () => document.querySelector("#feedback")?.textContent.length > 0,
  );
  assert.equal(await page.locator("#deposit").isDisabled(), true);
  console.log(
    "PASS default React build blocks signatures/transfers with live-enabled API; API failure remains disabled",
  );
} finally {
  await browser.close();
}
