import type {
  ApiErrorBody,
  BuyerView,
  DelegateEvent,
  DraftResult,
  Meta,
  OwnerRunView,
  SessionState,
} from "./types";

const TOKEN_KEY = "delegate.token";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage disabled: the session lasts for this page only */
  }
}

export class ApiError extends Error {
  status: number;
  code: string;
  retryable: boolean;
  requestId?: string;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.status = status;
    this.code = body.code;
    this.retryable = body.retryable;
    this.requestId = body.request_id;
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  form?: FormData;
  headers?: Record<string, string>;
  auth?: boolean;
  signal?: AbortSignal;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...opts.headers };
  if (opts.auth !== false) {
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(path, { method: opts.method ?? "GET", headers, body, signal: opts.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, { code: "NETWORK", message: "Could not reach the Delegate server. Check your connection and try again.", retryable: true });
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const b = (data ?? {}) as Partial<ApiErrorBody>;
    throw new ApiError(res.status, {
      code: b.code ?? `HTTP_${res.status}`,
      message: b.message ?? `The server answered ${res.status}.`,
      retryable: b.retryable ?? res.status >= 500,
      request_id: b.request_id ?? res.headers.get("X-Request-Id") ?? undefined,
    });
  }
  return data as T;
}

export function newKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export const api = {
  meta: () => request<Meta>("/v1/meta", { auth: false }),
  createSession: () => request<{ session_id: string; token: string; item_id: string }>("/v1/demo-sessions", { method: "POST", auth: false }),
  current: () => request<SessionState>("/v1/demo-sessions/current"),
  deleteSession: () => request<void>("/v1/demo-sessions/current", { method: "DELETE" }),
  patchMandate: (id: string, ifMatch: number, changes: Record<string, unknown>) =>
    request<{ mandate_id: string; revision: number }>(`/v1/mandates/${id}`, { method: "PATCH", body: changes, headers: { "If-Match": String(ifMatch) } }),
  activate: (id: string, revision: number) =>
    request<{ mandate_id: string; revision: number; expires_at: string }>(`/v1/mandates/${id}/activate`, { method: "POST", body: { revision } }),
  mandateAction: (id: string, action: "pause" | "resume" | "revoke") =>
    request<{ status: string; changed: boolean; revoked_at?: string }>(`/v1/mandates/${id}/${action}`, { method: "POST" }),
  draftText: (goal: string) => request<DraftResult>("/v1/mandate-drafts", { method: "POST", body: { goal } }),
  draftVoice: (wav: Blob) => {
    const form = new FormData();
    form.append("audio", wav, "voice-note.wav");
    return request<DraftResult>("/v1/mandate-drafts/voice", { method: "POST", form });
  },
  startRun: (buyerId: string) => request<{ run_id: string; created: boolean }>("/v1/runs", { method: "POST", body: { buyer_id: buyerId } }),
  run: (runId: string, signal?: AbortSignal) => request<OwnerRunView>(`/v1/runs/${runId}`, { signal }),
  buyerView: (runId: string, signal?: AbortSignal) => request<BuyerView>(`/v1/runs/${runId}/buyer-view`, { signal }),
  buyerTurn: (runId: string, text: string, key: string) =>
    request<{ event_id: string; risk_signals: string[] }>(`/v1/runs/${runId}/buyer-turns`, { method: "POST", body: { text }, headers: { "Idempotency-Key": key } }),
  acceptOffer: (runId: string, offerId: string, key: string) =>
    request<{ event_id: string; created: boolean }>(`/v1/runs/${runId}/accept-offer`, { method: "POST", body: { offer_id: offerId }, headers: { "Idempotency-Key": key } }),
  resumeRun: (runId: string) => request<{ run_id: string; state: string }>(`/v1/runs/${runId}/resume`, { method: "POST" }),
  resolveApproval: (approvalId: string, decision: "approve" | "reject", actionHash: string, key: string) =>
    request<Record<string, unknown>>(`/v1/approvals/${approvalId}/resolve`, {
      method: "POST",
      body: { decision, action_hash: actionHash },
      headers: { "Idempotency-Key": key },
    }),
  events: (runId: string, after: number, signal?: AbortSignal) =>
    request<{ events: DelegateEvent[] }>(`/v1/events?after=${after}&run_id=${encodeURIComponent(runId)}`, { signal }),
  evidence: (runId: string, view: "owner" | "buyer") => request<Record<string, unknown>>(`/v1/runs/${runId}/evidence?view=${view}`),
  evaluation: () => request<Record<string, unknown>>("/v1/evaluations/latest", { auth: false }),
};

/**
 * Server-sent events over fetch, so the Authorization header can be sent (EventSource cannot).
 * Reconnects with Last-Event-ID; after repeated stream failures it falls back to polling /v1/events.
 */
export function streamRunEvents(
  runId: string,
  startAfter: number,
  onEvent: (e: DelegateEvent) => void,
  onStatus: (s: "live" | "reconnecting" | "polling") => void,
): () => void {
  const ctrl = new AbortController();
  let cursor = startAfter;
  let failures = 0;
  let polls = 0;
  let stopped = false;

  const sleep = (ms: number) =>
    new Promise<void>((resolve) => {
      const t = setTimeout(resolve, ms);
      ctrl.signal.addEventListener("abort", () => {
        clearTimeout(t);
        resolve();
      });
    });

  const deliver = (e: DelegateEvent) => {
    if (e.seq <= cursor) return;
    cursor = e.seq;
    onEvent(e);
  };

  async function streamOnce(): Promise<void> {
    const token = getToken();
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (cursor > 0) headers["Last-Event-ID"] = String(cursor);
    const res = await fetch(`/v1/runs/${runId}/events?after=${cursor}`, { headers, signal: ctrl.signal, cache: "no-store" });
    if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
    onStatus("live");
    failures = 0;
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value;
      let idx: number;
      while ((idx = buf.search(/\r?\n\r?\n/)) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx).replace(/^\r?\n\r?\n/, "");
        const dataLines = block
          .split(/\r?\n/)
          .filter((l) => l.startsWith("data:"))
          .map((l) => l.slice(5).replace(/^ /, ""));
        if (!dataLines.length) continue;
        try {
          deliver(JSON.parse(dataLines.join("\n")) as DelegateEvent);
        } catch {
          /* ignore a malformed frame; the next refetch reconciles state */
        }
      }
    }
  }

  async function pollOnce(): Promise<void> {
    const { events } = await api.events(runId, cursor, ctrl.signal);
    events.forEach(deliver);
  }

  (async () => {
    while (!stopped) {
      try {
        if (failures < 3) {
          await streamOnce();
          if (stopped) return;
          onStatus("reconnecting");
          await sleep(1000);
        } else {
          onStatus("polling");
          await pollOnce();
          await sleep(1500);
          // Every 20 polls (about 30 s), give the stream another chance.
          polls += 1;
          if (polls % 20 === 0) failures = 0;
        }
      } catch {
        if (stopped) return;
        failures += 1;
        onStatus(failures >= 3 ? "polling" : "reconnecting");
        await sleep(Math.min(5000, 500 * 2 ** failures));
      }
    }
  })();

  return () => {
    stopped = true;
    ctrl.abort();
  };
}

export async function downloadEvidence(runId: string, view: "owner" | "buyer"): Promise<void> {
  const data = await api.evidence(runId, view);
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `delegate-evidence-${view}-${runId}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
