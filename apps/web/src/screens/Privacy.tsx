// Plain-language data notice. Keep in sync with docs/SECURITY_AND_DATA.md.
export function Privacy() {
  return (
    <main id="main" className="page page-eval" data-testid="privacy-page">
      <h1>Privacy and data</h1>
      <p className="lede">
        Delegate is a demonstration with a fictional laptop, fictional people and fictional money. Please do not type real
        personal information.
      </p>
      <h2>What is stored</h2>
      <ul>
        <li>The messages you type as the demo buyer, your mandate settings and the actions the agent took in your scenario.</li>
        <li>A random session token kept in this browser tab. There are no accounts and no cookies.</li>
        <li>A one-way hash of your network address, used only to limit how many new scenarios can be started per hour.</li>
      </ul>
      <h2>Where it goes</h2>
      <ul>
        <li>
          Buyer messages and the public listing are sent to Meta's Model API (Muse Spark, Standard mode) to choose the agent's next
          action. Your private minimum price is never sent.
        </li>
        <li>
          A voice note, if you record one, is sent to the same API once to suggest mandate fields. Delegate does not store the
          recording or its transcript.
        </li>
        <li>
          Delegate's own logs record timings and status codes, not message text. Google Cloud's standard request logs for the
          hosting service also record your IP address and the pages requested, under Google Cloud's default retention.
        </li>
      </ul>
      <h2>How long it is kept</h2>
      <ul>
        <li>Everything in a scenario is deleted when you click Reset or 24 hours after it was created, whichever comes first.</li>
        <li>The demo database lives inside a single server instance; a restart also erases it.</li>
      </ul>
      <h2>Age</h2>
      <p>This demo is for adults (18+), in line with Meta's Model API terms.</p>
    </main>
  );
}
