import { useEffect } from "react";
import { LiveRegion, Nav, Notices } from "./components/Chrome";
import { useRoute } from "./lib";
import { Evaluation } from "./screens/Evaluation";
import { Outcome } from "./screens/Outcome";
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
      {route === "/setup" ? <Setup /> : route === "/negotiate" ? <Workspace /> : route === "/outcome" ? <Outcome /> : route === "/evaluation" ? <Evaluation /> : <Start />}
      <footer className="footer">
        <span>{meta?.sandbox_label ?? "Marketplace sandbox: fictional item, people and money."}</span>
        <span className="mono">{meta?.release ? `release ${meta.release}` : ""}</span>
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
