import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, ApiError, getToken, setToken, streamRunEvents } from "./api";
import { navigate } from "./lib";
import type { BuyerView, DelegateEvent, Meta, OwnerRunView, SessionState } from "./types";

const RUN_KEY = "delegate.run";

export interface Notice {
  id: number;
  tone: "error" | "info" | "success";
  text: string;
  detail?: string;
}

export type StreamStatus = "idle" | "live" | "reconnecting" | "polling";

interface AppState {
  meta: Meta | null;
  metaError: string | null;
  session: SessionState | null;
  sessionLoading: boolean;
  runId: string | null;
  run: OwnerRunView | null;
  buyer: BuyerView | null;
  events: DelegateEvent[];
  stream: StreamStatus;
  notices: Notice[];
  liveText: string;
  ensureSession: () => Promise<SessionState | null>;
  refreshSession: () => Promise<SessionState | null>;
  refreshRun: () => Promise<void>;
  resetScenario: () => Promise<void>;
  selectRun: (runId: string | null) => void;
  announce: (text: string) => void;
  notify: (tone: Notice["tone"], text: string, detail?: string) => void;
  notifyError: (err: unknown, fallback?: string) => void;
  dismissNotice: (id: number) => void;
}

const Ctx = createContext<AppState | null>(null);

export function useApp(): AppState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp outside AppProvider");
  return v;
}

function readRunId(): string | null {
  try {
    return sessionStorage.getItem(RUN_KEY);
  } catch {
    return null;
  }
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [metaError, setMetaError] = useState<string | null>(null);
  const [session, setSession] = useState<SessionState | null>(null);
  const [sessionLoading, setSessionLoading] = useState<boolean>(() => Boolean(getToken()));
  const [runId, setRunId] = useState<string | null>(readRunId);
  const [run, setRun] = useState<OwnerRunView | null>(null);
  const [buyer, setBuyer] = useState<BuyerView | null>(null);
  const [events, setEvents] = useState<DelegateEvent[]>([]);
  const [stream, setStream] = useState<StreamStatus>("idle");
  const [notices, setNotices] = useState<Notice[]>([]);
  const [liveText, setLiveText] = useState("");
  const noticeSeq = useRef(0);
  const runIdRef = useRef(runId);
  runIdRef.current = runId;

  const announce = useCallback((text: string) => {
    // Clear first so repeating the same text is still announced.
    setLiveText("");
    window.setTimeout(() => setLiveText(text), 30);
  }, []);

  const dismissNotice = useCallback((id: number) => setNotices((n) => n.filter((x) => x.id !== id)), []);

  const notify = useCallback(
    (tone: Notice["tone"], text: string, detail?: string) => {
      const id = ++noticeSeq.current;
      setNotices((n) => [...n.slice(-3), { id, tone, text, detail }]);
      if (tone !== "error") window.setTimeout(() => dismissNotice(id), 6000);
    },
    [dismissNotice],
  );

  const notifyError = useCallback(
    (err: unknown, fallback = "Something went wrong.") => {
      if (err instanceof ApiError) notify("error", err.message, err.requestId ? `Request ${err.requestId} · ${err.code}` : err.code);
      else notify("error", fallback);
    },
    [notify],
  );

  const selectRun = useCallback((id: string | null) => {
    try {
      if (id) sessionStorage.setItem(RUN_KEY, id);
      else sessionStorage.removeItem(RUN_KEY);
    } catch {
      /* ignore */
    }
    setRunId(id);
  }, []);

  const loadSession = useCallback(async (): Promise<SessionState | null> => {
    if (!getToken()) {
      setSession(null);
      setSessionLoading(false);
      return null;
    }
    try {
      const s = await api.current();
      setSession(s);
      const known = runIdRef.current;
      if (known && !s.runs.some((r) => r.run_id === known)) selectRun(null);
      return s;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setToken(null);
        selectRun(null);
        setSession(null);
        return null;
      }
      throw err;
    } finally {
      setSessionLoading(false);
    }
  }, [selectRun]);

  const refreshSession = useCallback(async () => {
    try {
      return await loadSession();
    } catch (err) {
      notifyError(err, "Could not load your demo session.");
      return null;
    }
  }, [loadSession, notifyError]);

  const ensureSession = useCallback(async (): Promise<SessionState | null> => {
    setSessionLoading(true);
    try {
      const existing = await loadSession();
      if (existing) return existing;
      const created = await api.createSession();
      setToken(created.token);
      selectRun(null);
      return await loadSession();
    } catch (err) {
      setSessionLoading(false);
      notifyError(err, "Could not start a demo session.");
      return null;
    }
  }, [loadSession, notifyError, selectRun]);

  const resetScenario = useCallback(async () => {
    try {
      if (getToken()) await api.deleteSession().catch((e) => {
        if (!(e instanceof ApiError && e.status === 401)) throw e;
      });
    } catch (err) {
      notifyError(err, "Could not delete the old scenario.");
      return;
    }
    setToken(null);
    selectRun(null);
    setSession(null);
    setRun(null);
    setBuyer(null);
    setEvents([]);
    const s = await ensureSession();
    if (s) {
      announce("A new fictional scenario is ready. The old one was deleted.");
      notify("success", "New scenario ready. The previous one was deleted from the server.");
      navigate("/setup");
    }
  }, [announce, ensureSession, notify, notifyError, selectRun]);

  // Meta (mode chip) loads once.
  useEffect(() => {
    api
      .meta()
      .then(setMeta)
      .catch((err) => setMetaError(err instanceof ApiError ? err.message : "Could not reach the server."));
  }, []);

  // Restore a session from sessionStorage on load.
  useEffect(() => {
    if (getToken()) void refreshSession();
  }, [refreshSession]);

  // ---------------------------------------------------------------- run data: views + event stream
  const fetchTimer = useRef<number | null>(null);
  const fetchCtrl = useRef<AbortController | null>(null);

  const refreshRun = useCallback(async () => {
    const id = runIdRef.current;
    if (!id) return;
    fetchCtrl.current?.abort();
    const ctrl = new AbortController();
    fetchCtrl.current = ctrl;
    try {
      const [o, b] = await Promise.all([api.run(id, ctrl.signal), api.buyerView(id, ctrl.signal)]);
      if (runIdRef.current !== id) return;
      setRun(o);
      setBuyer(b);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (err instanceof ApiError && (err.status === 404 || err.status === 401)) {
        selectRun(null);
        return;
      }
      notifyError(err, "Could not load the negotiation.");
    }
  }, [notifyError, selectRun]);

  const scheduleRefresh = useCallback(() => {
    if (fetchTimer.current) window.clearTimeout(fetchTimer.current);
    fetchTimer.current = window.setTimeout(() => void refreshRun(), 120);
  }, [refreshRun]);

  useEffect(() => {
    setRun(null);
    setBuyer(null);
    setEvents([]);
    if (!runId || !session) {
      setStream("idle");
      return;
    }
    void refreshRun();
    const stop = streamRunEvents(
      runId,
      0,
      (e) => {
        setEvents((prev) => (prev.some((p) => p.seq === e.seq) ? prev : [...prev, e]));
        scheduleRefresh();
        if (e.type.startsWith("mandate.") || e.type.startsWith("receipt.")) void loadSession().catch(() => undefined);
      },
      setStream,
    );
    return () => {
      stop();
      setStream("idle");
    };
    // Re-subscribe only when the run or the session identity changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, session?.session_id]);

  const value = useMemo<AppState>(
    () => ({
      meta,
      metaError,
      session,
      sessionLoading,
      runId,
      run,
      buyer,
      events,
      stream,
      notices,
      liveText,
      ensureSession,
      refreshSession,
      refreshRun,
      resetScenario,
      selectRun,
      announce,
      notify,
      notifyError,
      dismissNotice,
    }),
    [meta, metaError, session, sessionLoading, runId, run, buyer, events, stream, notices, liveText, ensureSession, refreshSession, refreshRun, resetScenario, selectRun, announce, notify, notifyError, dismissNotice],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
