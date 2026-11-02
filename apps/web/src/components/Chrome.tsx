import { useEffect, useRef, type ReactNode } from "react";
import { navigate, useRoute, type Route } from "../lib";
import { useApp } from "../state";
import { Icon } from "./Icon";

export function ModeChip({ compact = false }: { compact?: boolean }) {
  const { meta, metaError } = useApp();
  let tone = "neutral";
  let text = "Checking model mode…";
  if (metaError) {
    tone = "danger";
    text = "Server unreachable";
  } else if (meta) {
    tone = meta.provider_mode === "live" ? "live" : meta.provider_mode === "mock" ? "mock" : "danger";
    text = meta.provider_label;
  }
  const title = meta
    ? meta.provider_mode === "live"
      ? `Negotiation model: ${meta.models.negotiation}. Voice notes: ${meta.models.voice_note}.`
      : meta.provider_mode === "mock"
        ? "No model is called. Deterministic local rules stand in so the product can be tried without a key."
        : "The live model is not configured. Nothing is faked: agent steps will report the provider as unavailable."
    : undefined;
  return (
    <span className={`mode-chip mode-${tone} ${compact ? "compact" : ""}`} data-testid="mode-chip" title={title}>
      <span className="dot" aria-hidden="true" />
      {text}
    </span>
  );
}

const LINKS: { to: Route; label: string; testid: string }[] = [
  { to: "/setup", label: "Mandate", testid: "nav-setup" },
  { to: "/negotiate", label: "Negotiation", testid: "nav-negotiate" },
  { to: "/outcome", label: "Outcome", testid: "nav-outcome" },
  { to: "/evaluation", label: "Evaluation", testid: "nav-evaluation" },
];

export function Nav() {
  const route = useRoute();
  const { session, resetScenario } = useApp();
  return (
    <header className="nav" role="banner">
      <div className="nav-inner">
        <a
          className="brand"
          href="/"
          onClick={(e) => {
            e.preventDefault();
            navigate("/");
          }}
          data-testid="nav-home"
        >
          <img src="/favicon.svg" alt="" width={28} height={28} />
          <span>Delegate</span>
        </a>
        <nav aria-label="Main" className="nav-links">
          {LINKS.map((l) => (
            <a
              key={l.to}
              href={l.to}
              data-testid={l.testid}
              aria-current={route === l.to ? "page" : undefined}
              onClick={(e) => {
                e.preventDefault();
                navigate(l.to);
              }}
            >
              {l.label}
            </a>
          ))}
        </nav>
        <div className="nav-right">
          <ModeChip compact />
          {session ? (
            <button
              type="button"
              className="btn btn-ghost-dark"
              onClick={() => void resetScenario()}
              aria-label="New scenario: delete this one and start fresh"
              title="Delete this scenario and start a fresh one"
              data-testid="reset-scenario"
            >
              <Icon name="refresh" size={18} />
              <span className="hide-sm">New scenario</span>
            </button>
          ) : null}
        </div>
      </div>
    </header>
  );
}

export function Notices() {
  const { notices, dismissNotice } = useApp();
  if (!notices.length) return null;
  return (
    <div className="notices" role="region" aria-label="Notifications">
      {notices.map((n) => (
        <div key={n.id} className={`notice notice-${n.tone}`} role={n.tone === "error" ? "alert" : "status"} data-testid={`notice-${n.tone}`}>
          <Icon name={n.tone === "error" ? "alert" : n.tone === "success" ? "checkCircle" : "info"} />
          <div>
            <div>{n.text}</div>
            {n.detail ? <div className="notice-detail">{n.detail}</div> : null}
          </div>
          <button type="button" className="icon-btn" aria-label="Dismiss notification" onClick={() => dismissNotice(n.id)}>
            <Icon name="x" size={18} />
          </button>
        </div>
      ))}
    </div>
  );
}

export function LiveRegion() {
  const { liveText } = useApp();
  return (
    <div className="sr-only" aria-live="polite" aria-atomic="true" data-testid="live-region">
      {liveText}
    </div>
  );
}

const FOCUSABLE = 'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Accessible modal dialog with a focus trap. Escape closes it unless `onClose` is undefined. */
export function Modal({
  labelledBy,
  describedBy,
  onClose,
  children,
  variant = "dialog",
  testid,
  initialFocus,
}: {
  labelledBy: string;
  describedBy?: string;
  onClose?: () => void;
  children: ReactNode;
  variant?: "dialog" | "drawer";
  testid?: string;
  initialFocus?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const node = ref.current;
    const first = (initialFocus ? node?.querySelector<HTMLElement>(initialFocus) : null) ?? node?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? node)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && closeRef.current) {
        e.preventDefault();
        closeRef.current();
        return;
      }
      if (e.key !== "Tab" || !node) return;
      const items = Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.offsetParent !== null || el === document.activeElement);
      if (!items.length) return;
      const a = items[0]!;
      const z = items[items.length - 1]!;
      if (e.shiftKey && document.activeElement === a) {
        e.preventDefault();
        z.focus();
      } else if (!e.shiftKey && document.activeElement === z) {
        e.preventDefault();
        a.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    document.body.classList.add("modal-open");
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("modal-open");
      prev?.focus?.();
    };
    // Mount-only: focus handling must not re-run on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className={`overlay overlay-${variant}`} onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}>
      <div
        ref={ref}
        className={`modal modal-${variant}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        tabIndex={-1}
        data-testid={testid}
      >
        {children}
      </div>
    </div>
  );
}
