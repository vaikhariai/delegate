// Response shapes of services/api (delegate/app.py and delegate/projections.py). Read-only mirror; the server decides.

export type ProviderMode = "live" | "mock" | "unavailable";

export interface Buyer {
  buyer_id: string;
  display_name: string;
}

export interface Meta {
  product: string;
  release: string | null;
  provider_mode: ProviderMode;
  provider_label: string;
  models: { negotiation: string; voice_note: string };
  sandbox_label: string;
  buyers: Buyer[];
}

export interface PublicFact {
  id: string;
  text: string;
}

export interface Listing {
  item_id: string;
  title: string;
  currency: string;
  asking_price_minor: number;
  public_facts: PublicFact[];
  state: string;
}

export interface MandateTerms {
  currency: string;
  asking_price_minor: number;
  min_price_minor: number;
  delivery: string;
  payment_terms: string;
  allowed_actions: string[];
  allowed_public_fact_ids: string[];
  reservation_requires_owner_approval: boolean;
  reservation_hold_seconds: number;
  max_reservations: number;
  max_outbound_messages: number;
  lifetime_seconds: number;
}

export type MandateStatus = "draft" | "active" | "paused" | "revoked" | "expired";

export interface Mandate {
  mandate_id: string;
  status: MandateStatus;
  current_revision: number;
  active_revision: number | null;
  activated_at: string | null;
  expires_at: string | null;
  terms: MandateTerms;
  active_terms: MandateTerms | null;
  has_unactivated_changes: boolean;
}

export interface RunSummary {
  run_id: string;
  buyer_id: string;
  buyer_display_name: string;
  state: string;
  created_at: string;
}

export interface SessionState {
  session_id: string;
  created_at: string;
  expires_at: string;
  provider_calls: number;
  buyer_turns: number;
  listing: Listing;
  mandate: Mandate;
  runs: RunSummary[];
}

export interface Message {
  seq: number;
  role: "buyer" | "seller" | "system";
  text: string;
  offer_id: string | null;
  at: string;
}

export interface Offer {
  offer_id: string;
  price_minor: number;
  currency: string;
  source: "listing" | "counter_offer" | string;
  display: string;
}

export interface Reservation {
  reservation_id: string;
  price_minor: number;
  currency: string;
  status: string;
  created_at: string;
  expires_at: string;
}

export interface PendingApproval {
  approval_id: string;
  action_id: string;
  action_hash: string;
  expires_at: string;
  mandate_revision: number;
  item_id: string;
  item_title: string;
  buyer_id: string;
  buyer_display_name: string;
  price_minor: number;
  currency: string;
  price_display: string;
  terms: { delivery: string; hold_seconds: number; payment_terms: string } | null;
  acceptance_event_id: string;
}

export interface Receipt {
  receipt_id: string;
  verdict: "verified" | "mismatch" | "unknown";
  mismatches: string[];
  fields: Record<string, string | number | null>;
  verified_at: string;
  operation_id: string;
}

export type RunState =
  | "awaiting_buyer"
  | "running"
  | "waiting_approval"
  | "blocked"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface OwnerRunView {
  run_id: string;
  state: RunState;
  state_reason: string | null;
  buyer_id: string;
  buyer_display_name: string;
  system: string;
  thinking: boolean;
  conversation: Message[];
  open_offer: Offer | null;
  reservation: Reservation | null;
  pending_approval: PendingApproval | null;
  receipt: Receipt | null;
  risk_signals: string[];
  provider_calls: number;
}

export interface BuyerView {
  run_id: string;
  buyer_display_name: string;
  listing: Listing;
  conversation: Message[];
  open_offer: Offer | null;
  reservation: Reservation | null;
  negotiation_open: boolean;
}

export interface DelegateEvent {
  seq: number;
  run_id: string | null;
  type: string;
  actor: string;
  object_id: string | null;
  summary: string;
  data: Record<string, unknown>;
  at: string;
}

export interface DraftResult {
  status: "suggestion_only";
  note?: string;
  suggestions: {
    asking_price_minor?: number;
    reservation_hold_seconds?: number;
    allowed_public_fact_ids?: string[];
  };
  ambiguities: string[];
  transcript: string;
  language_detected?: string | null;
  provider_mode: ProviderMode;
  model: string | null;
}

export type GateVerdict = "allow" | "deny" | "invalid" | "approval_required";

export interface LabCaseSummary {
  case_id: string;
  family: string;
  title: string;
  expected: { verdict: GateVerdict; reason_code: string | null };
  actual: { verdict: GateVerdict; reason_code: string | null };
  decided_by: string | null;
  pass: boolean;
}

export interface LabCaseList {
  evidence_mode: string;
  note: string;
  families: Record<string, string>;
  summary: { total: number; passed: number };
  cases: LabCaseSummary[];
}

export interface GateCheck {
  name: string;
  label: string;
  status: "passed" | "decided" | "not_run";
}

export interface GateTrace {
  verdict: GateVerdict;
  reason_code: string | null;
  decided_by: string | null;
  detail: string;
  checks: GateCheck[];
}

export interface LabCase {
  case_id: string;
  family: string;
  title: string;
  setup_overrides: Record<string, unknown>;
  effective_setup: Record<string, unknown>;
  proposal: unknown;
  expected: { verdict: GateVerdict; reason_code: string | null };
  trace: GateTrace;
}

export interface LabEvaluation {
  case_id: string;
  edited: boolean;
  trace: GateTrace;
  note: string;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  retryable: boolean;
  request_id?: string;
}
