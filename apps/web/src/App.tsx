import { useEffect } from "react";
import { LiveRegion, Nav, Notices } from "./components/Chrome";
import { navigate, useRoute } from "./lib";
import { Evaluation } from "./screens/Evaluation";
import { Outcome } from "./screens/Outcome";
import { Privacy } from "./screens/Privacy";
import { Setup } from "./screens/Setup";
import { Start } from "./screens/Start";
import { Workspace } from "./screens/Workspace";
import { AppProvider, useApp } from "./state";

const TITLES: Record<string, string> = {
  "/": "Delegate · agent commerce that holds up in production",
  "/setup": "Mandate · Delegate",
  "/negotiate": "Negotiation · Delegate",
  "/outcome": "Outcome and evidence · Delegate",
  "/evaluation": "Evaluation · Delegate",
  "/privacy": "Privacy and data · Delegate",
};

function Screens() {
  const route = useRoute();
  const { meta } = useApp();
  useEffect(() => {
    document.title = TITLES[route] ?? "Delegate";
  }, [route]);
  return (
    <>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <Nav />
      {route === "/setup" ? <Setup /> : route === "/negotiate" ? <Workspace /> : route === "/outcome" ? <Outcome /> : route === "/evaluation" ? <Evaluation /> : route === "/privacy" ? <Privacy /> : <Start />}
      <footer className="footer">
        <span>{meta?.sandbox_label ?? "Marketplace sandbox: fictional item, people and money."}</span>
        <span>
          <a href="/privacy" data-testid="privacy-link" onClick={(e) => { e.preventDefault(); navigate("/privacy"); }}>
            Privacy and data
          </a>
          <span className="mono">{meta?.release ? ` · release ${meta.release}` : ""}</span>
        </span>
      </footer>
      <Notices />
      <LiveRegion />
    </>
  );
}

export function App() {
  return (
    <AppProvider>
      <Screens />
    </AppProvider>
  );
}
