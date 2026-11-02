import { expect, type Page } from "@playwright/test";

/** Open the start page, begin a fresh scenario and land on the mandate screen. */
export async function startScenario(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByTestId("mode-chip").first()).toContainText("MOCK_ONLY");
  await page.getByTestId("start-button").click();
  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByTestId("activate-mandate")).toBeEnabled();
}

/** Activate the (default) mandate and wait for the workspace with a negotiation open. */
export async function activate(page: Page): Promise<void> {
  await page.getByTestId("activate-mandate").click();
  await expect(page).toHaveURL(/\/negotiate$/);
  await expect(page.getByTestId("authority-status")).toContainText("Active");
  await expect(page.getByTestId("chat-input")).toBeEnabled();
}

export async function startAndActivate(page: Page): Promise<void> {
  await startScenario(page);
  await activate(page);
}

/** Click a quick-prompt chip and wait for the agent's templated reply to appear in the buyer chat. */
export async function sendChipAndWait(page: Page, chip: string): Promise<string> {
  const seller = page.getByTestId("msg-seller");
  const before = await seller.count();
  await page.getByTestId(`chip-${chip}`).click();
  await expect(seller).toHaveCount(before + 1);
  return (await seller.nth(before).locator(".bubble").innerText()).trim();
}

export async function token(page: Page): Promise<string> {
  const t = await page.evaluate(() => sessionStorage.getItem("delegate.token"));
  expect(t, "session token in sessionStorage").toBeTruthy();
  return t!;
}

export async function runId(page: Page): Promise<string> {
  const r = await page.evaluate(() => sessionStorage.getItem("delegate.run"));
  expect(r, "run id in sessionStorage").toBeTruthy();
  return r!;
}

/** Call the API from inside the page (same origin, through the preview proxy) with an explicit token. */
export async function apiCall(
  page: Page,
  path: string,
  init: { method?: string; body?: unknown; token?: string | null } = {},
): Promise<{ status: number; body: unknown }> {
  return page.evaluate(
    async ({ path, init }) => {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (init.token) headers.Authorization = `Bearer ${init.token}`;
      const res = await fetch(path, { method: init.method ?? "GET", headers, body: init.body === undefined ? undefined : JSON.stringify(init.body) });
      const text = await res.text();
      let body: unknown = null;
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        body = text;
      }
      return { status: res.status, body };
    },
    { path, init },
  );
}

export async function noHorizontalOverflow(page: Page): Promise<void> {
  const { scroll, width } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, width: window.innerWidth }));
  expect(scroll, `page scrollWidth ${scroll} > viewport ${width}`).toBeLessThanOrEqual(width);
}

/** Close any toast notifications so screenshots show the product, not transient messages. */
export async function dismissNotices(page: Page): Promise<void> {
  const close = page.locator(".notice .icon-btn");
  while ((await close.count()) > 0) await close.first().click();
}
