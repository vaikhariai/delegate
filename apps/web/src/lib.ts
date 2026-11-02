import { useEffect, useState, useSyncExternalStore } from "react";

// ------------------------------------------------------------------ money and time

export function money(minor: number | null | undefined, currency = "USD"): string {
  if (minor === null || minor === undefined || !Number.isFinite(minor)) return "—";
  const sign = currency === "USD" ? "$" : `${currency} `;
  const dollars = Math.trunc(minor / 100);
  const cents = Math.abs(minor % 100);
  return `${sign}${dollars.toLocaleString("en-US")}${cents ? `.${String(cents).padStart(2, "0")}` : ""}`;
}

/** Parse a typed dollar amount ("520", "$520", "520.50") into integer cents, or null. */
export function parseDollars(text: string): number | null {
  const t = text.replace(/[$,\s]/g, "");
  if (!/^\d{1,6}(\.\d{1,2})?$/.test(t)) return null;
  const [d, c = ""] = t.split(".");
  return Number(d) * 100 + Number(c.padEnd(2, "0") || "0");
}

export function clockTime(iso: string | null | undefined, withSeconds = false): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", ...(withSeconds ? { second: "2-digit" } : {}) });
}

export function remaining(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null;
  return Math.max(0, Math.floor((new Date(iso).getTime() - now) / 1000));
}

export function mmss(seconds: number | null): string {
  if (seconds === null) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

// ------------------------------------------------------------------ tiny router (history API; the API serves index.html for any path)

export type Route = "/" | "/setup" | "/negotiate" | "/outcome" | "/evaluation" | "/lab" | "/privacy" | "/not-found";
const ROUTES: Route[] = ["/", "/setup", "/negotiate", "/outcome", "/evaluation", "/lab", "/privacy"];
const listeners = new Set<() => void>();

function currentRoute(): Route {
  const p = window.location.pathname.replace(/\/+$/, "") || "/";
  return (ROUTES as string[]).includes(p) ? (p as Route) : "/not-found";
}

window.addEventListener("popstate", () => listeners.forEach((l) => l()));

export function navigate(to: Route): void {
  if (window.location.pathname !== to) window.history.pushState({}, "", to);
  listeners.forEach((l) => l());
  window.scrollTo({ top: 0 });
}

export function useRoute(): Route {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    currentRoute,
    () => "/" as Route,
  );
}

// ------------------------------------------------------------------ labels

export const SIGNAL_LABELS: Record<string, { label: string; severity: "high" | "medium" | "low" }> = {
  qr_receive_scam: { label: "QR or payment-request scam", severity: "high" },
  fake_payment_proof: { label: "Claims payment already sent", severity: "high" },
  authority_claim: { label: "Claims authority or tells the agent to ignore rules", severity: "high" },
  off_platform_payment: { label: "Off-platform or advance payment", severity: "medium" },
  shipping_request: { label: "Asks for shipping or delivery", severity: "medium" },
  private_info_request: { label: "Asks for private details", severity: "medium" },
  off_platform_contact: { label: "Wants to move off the platform", severity: "low" },
};

export function signalLabel(s: string): string {
  return SIGNAL_LABELS[s]?.label ?? s.replace(/_/g, " ");
}

export const RUN_STATE_LABELS: Record<string, string> = {
  awaiting_buyer: "Waiting for the buyer",
  running: "Agent working",
  waiting_approval: "Waiting for your approval",
  blocked: "Blocked: needs a retry",
  paused: "Paused",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Ended",
};

export const MANDATE_STATUS_LABELS: Record<string, string> = {
  draft: "Draft · not active",
  active: "Active",
  paused: "Paused",
  revoked: "Revoked",
  expired: "Expired",
};

export function providerEvidenceMode(mode: string | undefined): string {
  if (mode === "live") return "LIVE_PROVIDER";
  if (mode === "mock") return "MOCK_ONLY";
  return "NOT_RUN";
}
