import { expect, test } from "@playwright/test";
import { startAndActivate } from "./helpers";

test("evaluation page shows the server's results with denominators, or an honest empty state", async ({ page, request }) => {
  const latest = (await (await request.get("/v1/evaluations/latest")).json()) as Record<string, unknown>;
  await page.goto("/evaluation");
  if (latest.status === "no_run_yet") {
    await expect(page.getByTestId("eval-empty")).toContainText("No evaluation run has been published yet");
    await expect(page.getByTestId("eval-empty")).not.toContainText("%");
    return;
  }
  const results = page.getByTestId("eval-results");
  await expect(results).toBeVisible();
  const gate = latest.gate_oracle_agreement as { numerator: number; denominator: number } | undefined;
  if (gate) await expect(results).toContainText(`${gate.numerator} / ${gate.denominator}`);
  const notRun = (latest.systems_not_run as string[] | undefined) ?? [];
  if (notRun.length) await expect(page.getByTestId("eval-not-run")).toContainText(notRun[0]!.toUpperCase());
  for (const l of ((latest.limitations as string[] | undefined) ?? []).slice(0, 2)) await expect(results).toContainText(l);
  const systems = Object.keys((latest.systems as Record<string, unknown> | undefined) ?? {});
  if (systems.includes("d")) {
    // Plain names, Delegate first.
    const titles = await results.locator(".card-title").allTextContents();
    const first = titles.findIndex((t) => t.includes("(system"));
    expect(titles[first]).toContain("Delegate");
    await expect(results).toContainText("Negotiations with a forbidden commitment");
  }
});

test("home page states the product shape and maps the reported incident to the checks", async ({ page }) => {
  await page.goto("/");
  const fits = page.getByTestId("where-it-fits");
  await expect(fits).toContainText("Buyer channel");
  await expect(fits).toContainText("Simulated in this demo");
  await expect(fits).toContainText("Seller console");
  await expect(fits).toContainText("never counts as the seller");
  await expect(page.getByTestId("incident-note")).toContainText("Muse");
  await expect(page.getByTestId("incident-map").locator("tbody tr")).toHaveCount(4);
  await expect(page.getByTestId("both-sides-note")).toContainText("You will play both sides");
  await expect(page.locator(".eyebrow").first()).toHaveText("Agent commerce with server-enforced limits");
});

test("the seller panel says why the private minimum is visible", async ({ page }) => {
  await startAndActivate(page);
  await expect(page.getByTestId("private-min-note").first()).toContainText("Hidden from the buyer and from the model");
});

test("privacy page explains data handling and is linked from every screen", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("privacy-link").click();
  await expect(page.getByTestId("privacy-page")).toContainText("deleted when you click Reset or 24 hours");
  await expect(page.getByTestId("privacy-page")).toContainText("private minimum price is never sent");
});

test("unknown pages show a not-found screen with a way back", async ({ page }) => {
  await page.goto("/no-such-page");
  await expect(page.getByTestId("not-found-page")).toBeVisible();
  await page.getByTestId("not-found-home").click();
  await expect(page.getByTestId("start-button")).toBeVisible();
});
