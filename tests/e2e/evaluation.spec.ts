import { expect, test } from "@playwright/test";

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
});
