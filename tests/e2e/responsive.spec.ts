import { expect, test } from "@playwright/test";
import { noHorizontalOverflow, sendChipAndWait, startAndActivate } from "./helpers";

for (const [width, height] of [
  [360, 780],
  [768, 1024],
  [1280, 800],
] as const) {
  test(`no horizontal overflow and reachable controls at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto("/");
    await noHorizontalOverflow(page);
    await startAndActivate(page);
    await noHorizontalOverflow(page);
    await sendChipAndWait(page, "offer");
    await noHorizontalOverflow(page);

    // Revoke is reachable without scrolling, even after the conversation has scrolled.
    await page.getByTestId("chat-input").scrollIntoViewIfNeeded();
    const revoke = page.getByTestId("revoke-button");
    await expect(revoke).toBeInViewport();
    await expect(page.getByTestId("pause-button")).toBeInViewport();

    if (width < 1024) {
      await expect(page.getByTestId("tab-chat")).toBeVisible();
      await page.getByTestId("tab-activity").click();
      await expect(page.getByTestId("activity-panel")).toBeVisible();
      await expect(page.getByTestId("buyer-panel")).toBeHidden();
      await expect(revoke).toBeInViewport();
      await page.getByTestId("tab-authority").click();
      await expect(page.getByTestId("authority-card")).toContainText("Counter range");
      await page.getByTestId("tab-chat").click();
      await expect(page.getByTestId("buyer-panel")).toBeVisible();
    } else {
      await expect(page.getByTestId("tab-chat")).toBeHidden();
      await expect(page.getByTestId("activity-panel")).toBeVisible();
      await expect(page.getByTestId("buyer-panel")).toBeVisible();
    }

    await page.getByTestId("accept-offer").click();
    await expect(page.getByTestId("approval-drawer")).toBeVisible();
    await expect(page.getByTestId("approve-button")).toBeVisible();
    await noHorizontalOverflow(page);
    await page.getByTestId("approve-button").click();
    await expect(page.getByTestId("receipt-panel")).toBeVisible();
    await noHorizontalOverflow(page);
    await page.getByTestId("nav-evaluation").click();
    await expect(page.getByTestId("eval-empty")).toBeVisible();
    await noHorizontalOverflow(page);
  });
}
