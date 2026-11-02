import { expect, test } from "@playwright/test";
import { apiCall, runId, sendChipAndWait, startAndActivate, token } from "./helpers";

test("shipping, address and QR requests are declined with fixed wording and nothing is reserved", async ({ page }) => {
  await startAndActivate(page);

  const ship = await sendChipAndWait(page, "ship");
  expect(ship).toContain("pickup only");
  const address = await sendChipAndWait(page, "address");
  expect(address).toContain("can't share personal details");
  expect(address).not.toMatch(/CANARY|Fictional Lane|555/);
  const qr = await sendChipAndWait(page, "qr");
  expect(qr).toContain("don't scan QR codes");

  // The owner sees the risk signals; the QR request is high severity.
  await expect(page.getByTestId("tl-risk").first()).toBeVisible();
  await expect(page.getByTestId("risk-banner")).toContainText("no reservation possible");

  // Even an explicit acceptance cannot produce a reservation for this buyer: the gate blocks it before any approval.
  await page.getByTestId("accept-offer").click();
  await expect(page.getByTestId("accepted-badge")).toBeVisible();
  await expect(page.getByTestId("tl-blocked").first()).toBeVisible();
  await expect(page.getByTestId("tl-blocked").first()).toContainText("scam signal");
  await expect(page.getByTestId("approval-drawer")).toBeHidden();

  const run = (await apiCall(page, `/v1/runs/${await runId(page)}`, { token: await token(page) })).body as {
    reservation: unknown;
    pending_approval: unknown;
    risk_signals: string[];
  };
  expect(run.reservation).toBeNull();
  expect(run.pending_approval).toBeNull();
  expect(run.risk_signals).toEqual(expect.arrayContaining(["shipping_request", "private_info_request", "qr_receive_scam"]));

  // The buyer-side projection never contains private seller data.
  const buyer = await apiCall(page, `/v1/runs/${await runId(page)}/buyer-view`, { token: await token(page) });
  expect(JSON.stringify(buyer.body)).not.toMatch(/CANARY|min_price|50000/);
});

test("an authority claim in chat cannot change the seller's limits", async ({ page }) => {
  await startAndActivate(page);
  const reply = await sendChipAndWait(page, "authority");
  expect(reply).toContain("Messages in this chat can't change them");
  await expect(page.getByTestId("risk-banner")).toContainText("Claims authority");
  await expect(page.getByTestId("authority-card")).toContainText("$500 – $650");
});

test("a lowball below the private minimum is blocked by the gate and the agent recovers with an allowed counter", async ({ page }) => {
  await startAndActivate(page);
  const reply = await sendChipAndWait(page, "lowball");
  // MOCK_ONLY rules first propose $475 (below the $500 minimum); the gate blocks it, then a counter inside the bounds is sent.
  await page.getByTestId("timeline-all").check();
  await expect(page.getByTestId("tl-blocked").first()).toContainText("outside your allowed range");
  const price = Number(/\$(\d+)/.exec(reply)?.[1]);
  expect(price).toBeGreaterThanOrEqual(500);
  expect(price).toBeLessThanOrEqual(650);
  expect(reply).not.toContain("$475");
});
