import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { noHorizontalOverflow, sendChipAndWait } from "./helpers";

// Findings from a 375x812 phone walkthrough.
test.use({ viewport: { width: 375, height: 812 } });

test("every nav item is visible without scrolling the nav", async ({ page }) => {
  await page.goto("/");
  for (const id of ["nav-setup", "nav-negotiate", "nav-outcome", "nav-evaluation", "nav-lab"]) {
    const link = page.getByTestId(id);
    await expect(link).toBeInViewport({ ratio: 1 });
  }
  await expect(page.getByRole("link", { name: "Lab" })).toBeVisible();
  await noHorizontalOverflow(page);
  await page.getByTestId("nav-lab").click();
  await expect(page).toHaveURL(/\/lab/);
});

test("the mandate screen has a pinned Activate button and new replies scroll into view", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  await page.getByTestId("start-button").click();
  const sticky = page.getByTestId("activate-mandate-sticky");
  await expect(sticky).toBeInViewport();
  await expect(sticky).toContainText("revision 1");
  await page.getByTestId("min-price").fill("700");
  await expect(sticky).toBeDisabled();
  await page.getByTestId("min-price").fill("520");
  await expect(sticky).toContainText("revision 2");
  await expect(sticky).toBeInViewport();
  const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);
  await sticky.click();
  await expect(page).toHaveURL(/\/negotiate$/);
  await expect(page.getByTestId("authority-revision")).toContainText("Revision 2");

  await sendChipAndWait(page, "available");
  // Scroll to the top so the offer bar is below the fold, then send without letting Playwright scroll anything.
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(page.getByTestId("offer-bar")).not.toBeInViewport();
  const sellers = await page.getByTestId("msg-seller").count();
  await page.getByTestId("chip-offer").dispatchEvent("click");
  await expect(page.getByTestId("msg-seller")).toHaveCount(sellers + 1);
  // The new offer and its Accept button are brought into view without the user scrolling.
  await expect(page.getByTestId("accept-offer")).toHaveText(/\$585/);
  await expect(page.getByTestId("accept-offer")).toBeInViewport();
  await expect(page.getByTestId("revoke-button")).toBeInViewport();
  await noHorizontalOverflow(page);
});
