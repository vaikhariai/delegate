import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { activate, apiCall, runId, sendChipAndWait, startAndActivate, startScenario, token } from "./helpers";

test.describe("happy path", () => {
  test("activate, ask, offer, accept, approve, verified receipt and evidence", async ({ page }) => {
    await startScenario(page);
    await expect(page.getByTestId("mandate-status")).toContainText("Draft");
    await expect(page.getByTestId("authority-preview")).toContainText("Counter between $500 and $650");
    await activate(page);
    await expect(page.getByTestId("authority-revision")).toContainText("Revision 1");

    const answer = await sendChipAndWait(page, "available");
    expect(answer).toContain("16 GB memory");
    await expect(page.getByTestId("tl-sent").first()).toBeVisible();

    const counter = await sendChipAndWait(page, "offer");
    expect(counter).toMatch(/I can do \$\d+ for pickup/);
    const accept = page.getByTestId("accept-offer");
    await expect(accept).toHaveText(/Accept offer \$585/);

    await accept.click();
    const drawer = page.getByTestId("approval-drawer");
    await expect(drawer).toBeVisible();
    await expect(page.getByTestId("approval-effect")).toContainText("Riya");
    await expect(page.getByTestId("approval-effect")).toContainText("$585");
    await expect(page.getByTestId("approval-effect")).toContainText("pickup only, cash at pickup, held 15 minutes");
    await expect(page.getByTestId("approval-revision")).toContainText("Revision 1");
    await expect(page.getByTestId("approve-button")).toBeFocused();

    await page.getByTestId("approve-button").click();
    await expect(page).toHaveURL(/\/outcome$/);
    await expect(page.getByTestId("outcome-status")).toContainText("Reserved in sandbox · Verified");
    await expect(page.getByTestId("outcome-status")).not.toContainText(/sold|paid/i);
    const receipt = page.getByTestId("receipt-panel");
    await expect(receipt).toHaveAttribute("data-verdict", "verified");
    for (const f of ["item_id", "buyer_id", "price_minor", "currency", "delivery", "payment_terms", "hold_seconds", "status"]) {
      await expect(page.getByTestId(`field-${f}`)).toHaveAttribute("data-state", "match");
    }
    await expect(page.getByTestId("field-price_minor")).toContainText("$585");
    await expect(page.getByTestId("prov-CONTROLLED_SANDBOX")).toBeVisible();
    await expect(page.getByTestId("prov-MOCK_ONLY")).toBeVisible();

    const ownerDl = page.waitForEvent("download");
    await page.getByTestId("download-owner-evidence").click();
    const owner = await ownerDl;
    expect(owner.suggestedFilename()).toMatch(/^delegate-evidence-owner-run_/);
    const ownerJson = JSON.parse(await readFile((await owner.path())!, "utf8"));
    expect(ownerJson.evidence_modes).toEqual(expect.arrayContaining(["CONTROLLED_SANDBOX", "MOCK_ONLY"]));
    expect(ownerJson.run.receipt.verdict).toBe("verified");

    const buyerDl = page.waitForEvent("download");
    await page.getByTestId("download-buyer-evidence").click();
    const buyerText = await readFile((await (await buyerDl).path())!, "utf8");
    expect(buyerText).not.toContain("min_price_minor");
    expect(buyerText).not.toContain("CANARY");
    expect(JSON.parse(buyerText).view).toBe("buyer");
  });

  test("Hinglish buyer gets a Hinglish counter-offer", async ({ page }) => {
    await startAndActivate(page);
    const reply = await sendChipAndWait(page, "hinglish");
    expect(reply).toContain("mein de sakta hoon");
    await expect(page.getByTestId("accept-offer")).toHaveText(/Accept offer \$585/);
  });
});

test("owner rejects the approval: nothing is reserved and the negotiation continues", async ({ page }) => {
  await startAndActivate(page);
  await sendChipAndWait(page, "offer");
  await page.getByTestId("accept-offer").click();
  await expect(page.getByTestId("approval-drawer")).toBeVisible();
  await page.getByTestId("reject-button").click();
  await expect(page.getByTestId("approval-drawer")).toBeHidden();
  await expect(page.getByTestId("tl-rejected")).toBeVisible();
  await expect(page).toHaveURL(/\/negotiate$/);
  await expect(page.getByTestId("run-state")).toContainText("Waiting for the buyer");
  const run = await apiCall(page, `/v1/runs/${await runId(page)}`, { token: await token(page) });
  expect((run.body as { reservation: unknown }).reservation).toBeNull();
  expect((run.body as { pending_approval: unknown }).pending_approval).toBeNull();
  await expect(page.getByTestId("chat-input")).toBeEnabled();
  await page.getByTestId("nav-outcome").click();
  await expect(page.getByTestId("outcome-status")).toContainText("No reservation");
});

test("closing the drawer keeps the request; Escape closes; Review reopens it", async ({ page }) => {
  await startAndActivate(page);
  await sendChipAndWait(page, "offer");
  await page.getByTestId("accept-offer").click();
  await expect(page.getByTestId("approval-drawer")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("approval-drawer")).toBeHidden();
  await expect(page.getByTestId("approval-banner")).toBeVisible();
  await page.getByTestId("review-approval").click();
  await expect(page.getByTestId("approval-drawer")).toBeVisible();
  // Focus stays inside the dialog while tabbing.
  for (let i = 0; i < 8; i++) await page.keyboard.press("Tab");
  expect(await page.evaluate(() => !!document.activeElement?.closest("[data-testid=approval-drawer]"))).toBe(true);
});

test("reset deletes the old scenario and starts a fresh one", async ({ page }) => {
  await startAndActivate(page);
  await sendChipAndWait(page, "available");
  const oldToken = await token(page);
  await page.getByTestId("reset-scenario").click();
  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByTestId("mandate-status")).toContainText("Draft");
  const newToken = await token(page);
  expect(newToken).not.toBe(oldToken);
  const old = await apiCall(page, "/v1/demo-sessions/current", { token: oldToken });
  expect(old.status).toBe(401);
  await activate(page);
  await expect(page.getByTestId("msg-seller")).toHaveCount(0);
  await expect(page.getByTestId("msg-buyer")).toHaveCount(0);
});

test("a refresh restores the negotiation from the server", async ({ page }) => {
  await startAndActivate(page);
  await sendChipAndWait(page, "available");
  await page.reload();
  await expect(page.getByTestId("msg-seller")).toHaveCount(1);
  await expect(page.getByTestId("authority-status")).toContainText("Active");
});

test("editing the mandate creates and activates a new revision", async ({ page }) => {
  await startScenario(page);
  await page.getByTestId("min-price").fill("520");
  await page.getByTestId("hold-minutes").fill("20");
  await expect(page.getByTestId("authority-preview")).toContainText("Counter between $520 and $650");
  await expect(page.getByTestId("activate-mandate")).toContainText("revision 2");
  await page.getByTestId("min-price").fill("700");
  await expect(page.getByTestId("activate-mandate")).toBeDisabled();
  await page.getByTestId("min-price").fill("520");
  await activate(page);
  await expect(page.getByTestId("authority-revision")).toContainText("Revision 2");
  await expect(page.getByTestId("authority-card")).toContainText("$520 – $650");
  await expect(page.getByTestId("authority-card")).toContainText("held 20 min");
});

test("text draft shows suggestions that only change the form when applied", async ({ page }) => {
  await startScenario(page);
  await page.getByTestId("draft-goal").fill("Sell it for $640, pickup only. Maybe ship if they insist.");
  await page.getByTestId("draft-submit").click();
  const result = page.getByTestId("draft-result");
  await expect(result).toContainText("Suggestions only · not active");
  await expect(result).toContainText("$640");
  await expect(result).toContainText("pickup only");
  await expect(page.getByTestId("asking-price")).toHaveValue("650");
  await page.getByTestId("apply-asking").click();
  await expect(page.getByTestId("asking-price")).toHaveValue("640");
  await expect(page.getByTestId("min-price")).toHaveValue("500");
  await expect(page.getByTestId("mandate-status")).toContainText("Draft");
});
