import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { sendChipAndWait, startScenario, activate } from "./helpers";

async function seriousViolations(page: Page, label: string) {
  const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const bad = result.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  const summary = bad.map((v) => `${label}: ${v.id} (${v.impact}) ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(", ")}`);
  expect(summary, summary.join("\n")).toEqual([]);
}

test("no serious or critical axe violations on any screen", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  await expect(page.getByTestId("mode-chip").first()).toContainText("MOCK_ONLY");
  await seriousViolations(page, "start");

  await startScenario(page);
  await seriousViolations(page, "setup");

  await activate(page);
  await sendChipAndWait(page, "qr");
  await sendChipAndWait(page, "offer");
  await seriousViolations(page, "workspace");

  await page.getByTestId("accept-offer").click();
  await expect(page.getByTestId("tl-blocked").first()).toBeVisible();
  await seriousViolations(page, "workspace with blocked reservation");

  await page.getByTestId("nav-outcome").click();
  await seriousViolations(page, "outcome (none)");

  await page.getByTestId("nav-evaluation").click();
  await expect(page.getByTestId(/^eval-(empty|results)$/)).toBeVisible();
  await seriousViolations(page, "evaluation");
});

test("approval drawer and verified outcome have no serious axe violations", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await startScenario(page);
  await activate(page);
  await sendChipAndWait(page, "offer");
  await page.getByTestId("accept-offer").click();
  await expect(page.getByTestId("approval-drawer")).toBeVisible();
  await seriousViolations(page, "approval drawer");
  await page.getByTestId("approve-button").click();
  await expect(page.getByTestId("receipt-panel")).toBeVisible();
  await seriousViolations(page, "outcome (verified)");
});

test("mobile workspace has no serious axe violations", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 780 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await startScenario(page);
  await activate(page);
  await sendChipAndWait(page, "available");
  await seriousViolations(page, "mobile workspace");
});
