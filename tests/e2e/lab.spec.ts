import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { noHorizontalOverflow } from "./helpers";

const COUNTER_40000 = {
  kind: "send_message",
  message_intent: "counter_offer",
  fact_ids: [],
  price_minor: 40000,
  currency: "USD",
  clarify_topic: null,
  decline_reason: null,
  acceptance_event_id: null,
  language: "en",
  buyer_risk_signals: [],
  rationale: "Edited in the gate lab: a counter below the private minimum.",
};

test("gate lab lists every case, filters by family, and replays an edited proposal check by check", async ({ page }) => {
  await page.goto("/evaluation");
  await page.getByTestId("eval-open-lab").click();
  await expect(page).toHaveURL(/\/lab/);
  await expect(page.getByTestId("lab-evidence-mode")).toHaveText("SYNTHETIC_FIXTURE");
  await expect(page.getByTestId("lab-summary")).toContainText("151/151 match the expectation written from the policy");
  const rows = page.locator("[data-testid^=lab-case-]");
  expect(await rows.count()).toBeGreaterThanOrEqual(151);
  await expect(page.getByTestId("lab-count")).toContainText("of 151 cases");

  // Family filter.
  await page.getByTestId("lab-family").selectOption("D06");
  const d06 = await rows.count();
  expect(d06).toBeGreaterThan(0);
  expect(d06).toBeLessThan(151);
  for (const id of await rows.evaluateAll((els) => els.map((e) => e.getAttribute("data-testid")))) expect(id).toMatch(/^lab-case-D06-/);
  await page.getByTestId("lab-family").selectOption("");
  await page.getByTestId("lab-verdict").selectOption("approval_required");
  for (const chip of await rows.locator(".verdict").allInnerTexts()) expect(chip).toContain("Needs approval");
  await page.getByTestId("lab-verdict").selectOption("");
  await page.getByTestId("lab-search").fill("D01-01");
  await expect(rows).toHaveCount(1);

  // Open D01-01: the frozen proposal is allowed with every check passed.
  await page.getByTestId("lab-case-D01-01").click();
  await expect(page.getByTestId("lab-detail-title")).toContainText("D01-01");
  await expect(page.getByTestId("lab-result-verdict")).toHaveText("Allow");
  await expect(page.getByTestId("lab-overrides")).toContainText("None. Default setup");

  // Edit it into a $400 counter: the money check decides, earlier checks passed, later ones never run.
  await page.getByTestId("lab-proposal").fill(JSON.stringify(COUNTER_40000, null, 2));
  await page.getByTestId("lab-evaluate").click();
  await expect(page.getByTestId("lab-result")).toHaveAttribute("data-edited", "true");
  await expect(page.getByTestId("lab-result-verdict")).toHaveText("Deny");
  await expect(page.getByTestId("lab-result-code")).toHaveText("PRICE_OUT_OF_BOUNDS");
  await expect(page.getByTestId("lab-result-reason")).toContainText("below the private minimum");
  const decided = page.locator("[data-testid^=lab-check-][data-status=decided]");
  await expect(decided).toHaveCount(1);
  await expect(decided).toContainText("Currency and price bounds");
  for (const name of ["1_schema", "2_scope", "3_fence_and_run", "4_authority", "5_allowed_and_budget"]) {
    await expect(page.getByTestId(`lab-check-${name}`)).toHaveAttribute("data-status", "passed");
  }
  for (const name of ["7_terms_disclosure_risk", "8_acceptance", "9_duplicate", "10_approval"]) {
    await expect(page.getByTestId(`lab-check-${name}`)).toHaveAttribute("data-status", "not_run");
  }

  // Reset restores the frozen proposal and its result.
  await page.getByTestId("lab-reset").click();
  await expect(page.getByTestId("lab-result-verdict")).toHaveText("Allow");
});

test("invalid JSON shows an inline error and sends no request", async ({ page }) => {
  let posted = 0;
  page.on("request", (r) => {
    if (r.url().includes("/v1/lab/evaluate")) posted += 1;
  });
  await page.goto("/lab?case=D01-01");
  await expect(page.getByTestId("lab-detail-title")).toContainText("D01-01");
  await page.getByTestId("lab-proposal").fill('{"kind": "send_message",');
  await page.getByTestId("lab-evaluate").click();
  await expect(page.getByTestId("lab-json-error")).toContainText("not valid JSON");
  await expect(page.getByTestId("lab-proposal")).toHaveAttribute("aria-invalid", "true");
  await page.waitForTimeout(500);
  expect(posted).toBe(0);
});

test("a case with setup overrides shows them as readable lines", async ({ page }) => {
  await page.goto("/lab?case=D21-01");
  await expect(page.getByTestId("lab-overrides")).toContainText("Mandate expires in (s)");
  await expect(page.getByTestId("lab-overrides")).toContainText("-1");
  await expect(page.getByTestId("lab-check-4_authority")).toHaveAttribute("data-status", "decided");
});

test("gate lab has no serious axe violations and fits a 360 px screen", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/lab?case=D01-01");
  await expect(page.getByTestId("lab-result-verdict")).toBeVisible();
  const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const bad = result.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => `${v.id}: ${v.nodes[0]?.target}`);
  expect(bad, bad.join("\n")).toEqual([]);

  await page.setViewportSize({ width: 360, height: 780 });
  await page.reload();
  await expect(page.getByTestId("lab-result-verdict")).toBeVisible();
  await noHorizontalOverflow(page);
  // List above detail on a phone.
  const listBox = await page.getByTestId("lab-cases").boundingBox();
  const detailBox = await page.getByTestId("lab-detail").boundingBox();
  expect(listBox!.y).toBeLessThan(detailBox!.y);
  const mobile = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(mobile.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);
});
