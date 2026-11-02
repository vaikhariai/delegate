import { expect, test } from "@playwright/test";
import { apiCall, runId, sendChipAndWait, startAndActivate, token } from "./helpers";

test("two browser contexts get isolated sessions", async ({ browser }) => {
  const a = await browser.newContext();
  const b = await browser.newContext();
  const pa = await a.newPage();
  const pb = await b.newPage();
  try {
    await Promise.all([startAndActivate(pa), startAndActivate(pb)]);
    const [ta, tb] = [await token(pa), await token(pb)];
    expect(ta).not.toBe(tb);

    await sendChipAndWait(pa, "offer");
    await expect(pa.getByTestId("accept-offer")).toHaveText(/\$585/);

    // B sees none of A's conversation.
    await pb.waitForTimeout(1000);
    await expect(pb.getByTestId("msg-buyer")).toHaveCount(0);
    await expect(pb.getByTestId("msg-seller")).toHaveCount(0);

    // B cannot read or act on A's negotiation, even with its id.
    const aRun = await runId(pa);
    const read = await apiCall(pb, `/v1/runs/${aRun}`, { token: tb });
    expect(read.status).toBe(404);
    const write = await apiCall(pb, `/v1/runs/${aRun}/buyer-turns`, { method: "POST", body: { text: "hello" }, token: tb });
    expect(write.status).toBe(404);
    const events = await apiCall(pb, `/v1/events?after=0&run_id=${aRun}`, { token: tb });
    expect(events.status).toBe(404);

    // Revoking in B leaves A active.
    await pb.getByTestId("revoke-button").click();
    await pb.getByTestId("revoke-confirm").click();
    await expect(pb.getByTestId("authority-status")).toContainText("Revoked");
    await pa.reload();
    await expect(pa.getByTestId("authority-status")).toContainText("Active");
    await expect(pa.getByTestId("accept-offer")).toBeEnabled();
  } finally {
    await a.close();
    await b.close();
  }
});
