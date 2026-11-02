import { expect, test } from "@playwright/test";
import { startScenario } from "./helpers";

// Chromium's fake microphone (a beeping test tone) stands in for a real voice. This proves the browser records,
// converts to 16 kHz mono 16-bit WAV and the server accepts it; in MOCK_ONLY mode no transcription happens.
test.use({
  launchOptions: { args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] },
  permissions: ["microphone"],
});

test("voice note is recorded, encoded as WAV in the browser and returns inactive suggestions", async ({ page }) => {
  // Observe the uploaded file in the page: Chromium does not expose multipart Blob bodies to request.postDataBuffer().
  await page.addInitScript(() => {
    const orig = window.fetch.bind(window);
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const body = init?.body;
      if (body instanceof FormData && body.get("audio") instanceof Blob) {
        const blob = body.get("audio") as Blob;
        const head = new Uint8Array(await blob.slice(0, 44).arrayBuffer());
        (window as unknown as { __wav: unknown }).__wav = { size: blob.size, type: blob.type, head: Array.from(head) };
      }
      return orig(input, init);
    };
  });
  await startScenario(page);
  const response = page.waitForResponse((r) => r.url().includes("/v1/mandate-drafts/voice") && r.request().method() === "POST");
  await page.getByTestId("voice-record").click();
  await expect(page.getByTestId("voice-stop")).toBeVisible();
  await page.waitForTimeout(1500);
  await page.getByTestId("voice-stop").click();
  expect((await response).status()).toBe(200);

  const wav = (await page.evaluate(() => (window as unknown as { __wav: { size: number; type: string; head: number[] } }).__wav))!;
  const head = Buffer.from(wav.head);
  expect(head.subarray(0, 4).toString("latin1")).toBe("RIFF");
  expect(head.subarray(8, 12).toString("latin1")).toBe("WAVE");
  expect(head.readUInt16LE(20)).toBe(1); // PCM
  expect(head.readUInt16LE(22)).toBe(1); // mono
  expect(head.readUInt32LE(24)).toBe(16000); // 16 kHz
  expect(head.readUInt16LE(34)).toBe(16); // 16-bit
  expect(wav.size).toBeGreaterThan(16000); // at least ~0.5 s of audio
  expect(wav.size).toBeLessThan(4 * 1024 * 1024);

  await expect(page.getByTestId("draft-result")).toContainText("Suggestions only · not active");
  await expect(page.getByTestId("mandate-status")).toContainText("Draft");
});
