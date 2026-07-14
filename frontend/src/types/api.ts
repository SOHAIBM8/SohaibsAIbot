/**
 * TypeScript types mirroring api/schemas/*.py field-for-field (spec
 * section 4's own framing, applied to this boundary too) — trading
 * semantics are never redefined here, only typed for the frontend.
 */

// ---- auth ----

export interface SessionInfo {
  account_id: string;
  expires_at: string;
}

// ---- experiments ----

export interface ExperimentConfigOut {
  strategy_ids: string[];
  symbol: string;
  timeframe: string;
  date_range: [string, string];
  feature_pipeline_version: string;
  fee_bps: number;
  slippage_model: string;
  code_commit_hash: string;
  risk_config_id: string | null;
}

export interface ExperimentResultOut {
  experiment_id: number;
  config: ExperimentConfigOut;
  started_at: string;
  finished_at: string | null;
  metrics: Record<string, number>;
  equity_curve_path: string | null;
  notes: string;
}

export interface ComparisonTableOut {
  results: ExperimentResultOut[];
}

// ---- risk ----

export interface RiskConfigOut {
  risk_config_id: string;
  version: string;
  daily_loss_limit_pct: number;
  weekly_loss_limit_pct: number;
  drawdown_tier_1_pct: number;
  drawdown_tier_1_factor: number;
  drawdown_tier_2_pct: number;
  drawdown_tier_3_pct: number;
  max_gross_exposure_pct: number;
  max_net_exposure_pct: number;
  max_concurrent_positions: number;
  max_same_symbol_directional_exposure_pct: number;
  sizing_method: string;
  kelly_fraction_multiplier: number;
  kelly_min_sample_size: number;
  circuit_breaker_atr_percentile_threshold: number;
  circuit_breaker_confirmation_bars: number;
  kill_switch_auto_flatten: boolean;
}

export interface KillSwitchStateOut {
  scope: string;
  engaged: boolean;
  engaged_at: string | null;
  engaged_reason: string | null;
  engaged_by: string | null;
  updated_at: string | null;
}

export interface CircuitBreakerStateOut {
  breaker_name: string;
  tripped: boolean;
  reason: string | null;
  occurred_at: string;
}

export interface LayerResultOut {
  layer_name: string;
  passed: boolean;
  multiplier: number;
  reason: string | null;
}

export interface ArmingStateOut {
  account_id: string;
  strategy_id: string;
  exchange: string;
  armed: boolean;
  armed_at: string | null;
  expires_at: string | null;
  armed_by: string | null;
  mainnet: boolean;
}

export interface RiskDecisionRecordOut {
  id: number;
  experiment_id: number | null;
  bar_time: string;
  strategy_id: string;
  proposed_quantity: number;
  approved_quantity: number;
  rejection_reason: string | null;
  throttle_reasons: string[];
  layer_results: LayerResultOut[];
  risk_config_id: string | null;
}

// ---- orders / positions ----

export interface FillOut {
  id: number;
  client_order_id: string;
  fill_price: number;
  quantity: number;
  fee: number;
  is_partial: boolean;
  filled_at: string;
}

export interface OrderOut {
  client_order_id: string;
  exchange_order_id: string | null;
  account_id: string | null;
  strategy_id: string;
  symbol: string;
  order_type: string;
  direction: number;
  quantity: number;
  limit_price: number | null;
  stop_price: number | null;
  mode: string;
  state: string;
  risk_decision_id: number;
  created_at: string;
  updated_at: string;
}

export interface OrderDetailOut extends OrderOut {
  fills: FillOut[];
}

export interface PositionsResponseOut {
  available: boolean;
  reason: string | null;
  positions: Record<string, unknown>[];
}

// ---- portfolio ----

export interface AccountOut {
  account_id: string;
  starting_balance: number;
  current_cash: number;
  created_at: string;
}

export interface EquitySnapshotOut {
  id: number;
  account_id: string;
  equity: number;
  open_position_count: number;
  snapshot_at: string;
}

export interface EquityCurveResponseOut {
  available: boolean;
  reason: string | null;
  snapshots: EquitySnapshotOut[];
}

// ---- market ----

export interface CandleOut {
  exchange: string;
  symbol: string;
  timeframe: string;
  open_time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

// ---- AI assistant ----

export interface ChatResponseOut {
  answer: string;
}

export interface ExplanationOut {
  explanation_id: number;
  subject_type: string;
  subject_id: string;
  generated_text: string;
  prompt_version: string;
  generated_at: string;
}

// ---- settings ----

export interface CredentialCreateIn {
  exchange: string;
  api_key: string;
  api_secret: string;
  mainnet: boolean;
}

export interface CredentialSummaryOut {
  credential_id: string;
  exchange: string;
  mainnet: boolean;
  state: string;
  created_at: string;
  last_validated_at: string | null;
  last_rotated_at: string | null;
  rotation_due_at: string | null;
}

export interface NotificationPreferencesOut {
  account_id: string;
  email_enabled: boolean;
  email_address: string | null;
  webhook_enabled: boolean;
  webhook_url: string | null;
  notify_on_kill_switch: boolean;
  notify_on_credential_validation_failed: boolean;
  notify_on_drawdown_breach: boolean;
  updated_at: string | null;
}

export type NotificationPreferencesIn = Omit<NotificationPreferencesOut, "account_id" | "updated_at">;

// ---- notifications ----

export interface NotificationOut {
  id: number;
  event_type: string;
  severity: "critical" | "warning" | "info";
  message: string;
  payload: Record<string, unknown>;
  occurred_at: string;
}

// ---- dashboard overview ----

export interface UnavailableOut {
  available: false;
  reason: string;
}

export interface LatestDailySummaryOut {
  explanation_id: number;
  subject_id: string;
  generated_text: string;
  generated_at: string;
}

export interface DashboardOverviewOut {
  mode: UnavailableOut;
  open_position_count: UnavailableOut;
  today_pnl: UnavailableOut;
  equity_curve: EquityCurveResponseOut;
  recent_risk_decisions: RiskDecisionRecordOut[];
  latest_daily_summary: LatestDailySummaryOut | null;
}
