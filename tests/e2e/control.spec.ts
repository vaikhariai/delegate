import { expect, test } from "@playwright/test";
import { apiCall, runId, sendChipAndWait, startAndActivate, token } from "./helpers";

test("revoke during a negotiation stops the agent; later messages cannot be sent", async ({ page }) => {
  await startAndActivate(page);
  await sendChipAndWait(page, "available");
  await sendChipAndWait(page, "offer");

  await page.getByTestId("revoke-button").click();
  const dialog = page.getByTestId("revoke-dialog");
  await expect(dialog).toBeVisible();
  await expect(page.getByTestId("revoke-cancel")).toBeFocused();
  await page.getByTestId("revoke-confirm").click();
  await expect(dialog).toBeHidden();

  await expect(page.getByTestId("authority-status")).toContainText("Revoked");
  await expect(page.getByTestId("revoked-confirmation")).toContainText(/Revoked at \d{1,2}:\d{2}:\d{2}/);
  await expect(page.getByTestId("revoked-confirmation")).toContainText("confirmed by the server");
  await expect(page.getByTestId("tl-revoked")).toBeVisible();
  await expect(page.getByTestId("negotiation-ended")).toContainText("revoked");
  await expect(page.getByTestId("chat-input")).toBeDisabled();
  await expect(page.getByTestId("send-button")).toBeDisabled();
  await expect(page.getByTestId("chip-available")).toBeDisabled();
  await expect(page.getByTestId("revoke-button")).toBeDisabled();
  await expect(page.getByTestId("accept-offer")).toHaveCount(0);

  // The server, not just the UI, refuses a later buyer message and any acceptance.
  const id = await runId(page);
  const t = await token(page);
  const later = await apiCall(page, `/v1/runs/${id}/buyer-turns`, { method: "POST", body: { text: "Still there? I'll take it." }, token: t });
  expect(later.status).toBe(409);
  expect((later.body as { code: string }).code).toBe("RUN_ENDED");
  const seller = await page.getByTestId("msg-seller").count();
  await page.waitForTimeout(1500);
  await expect(page.getByTestId("msg-seller")).toHaveCount(seller);
});

test("revoking while an approval is pending cancels it", async ({ page }) => {
  await startAndActivate(page);
  await sendChipAndWait(page, "offer");
  await page.getByTestId("accept-offer").click();
  await expect(page.getByTestId("approval-drawer")).toBeVisible();
  await page.getByTestId("approval-close").click();
  await page.getByTestId("revoke-button").click();
  await page.getByTestId("revoke-confirm").click();
  await expect(page.getByTestId("authority-status")).toContainText("Revoked");
  await expect(page.getByTestId("approval-banner")).toHaveCount(0);
  await expect(page.getByTestId("tl-approval-cancelled")).toBeVisible();
  const run = (await apiCall(page, `/v1/runs/${await runId(page)}`, { token: await token(page) })).body as { reservation: unknown; state: string };
  expect(run.reservation).toBeNull();
  expect(run.state).toBe("cancelled");
});

test("pause stops replies until resume", async ({ page }) => {
  await startAndActivate(page);
  await page.getByTestId("pause-button").click();
  await expect(page.getByTestId("authority-status")).toContainText("Paused");
  await expect(page.getByTestId("tl-paused")).toBeVisible();
  await page.getByTestId("chip-available").click();
  await expect(page.getByTestId("msg-buyer")).toHaveCount(1);
  await page.waitForTimeout(2000);
  await expect(page.getByTestId("msg-seller")).toHaveCount(0);
  await page.getByTestId("resume-button").click();
  await expect(page.getByTestId("authority-status")).toContainText("Active");
  // The message that arrived while paused is answered once the agent resumes.
  await expect(page.getByTestId("msg-seller")).toHaveCount(1, { timeout: 20_000 });
  await expect(page.getByTestId("msg-seller").first()).toContainText("still available");
  const reply = await sendChipAndWait(page, "battery");
  expect(reply).toContain("Battery health is 87%");
});
