# Risk Management Engine — implementation specification

Status: approved architecture, ready for implementation.
Read alongside `CLAUDE.md` and `docs/historical_data_ingestion_spec.md`.
The 10 project rules and working style apply unchanged.

## 1. Locked-in decisions (this phase)

| # | Decision |
|---|----------|
| 1 | UTC is the global time boundary. Daily = 00:00–23:59 UTC. Weekly = Monday 00:00 UTC – Sunday 23:59 UTC. |
| 2 | Kill switch default = block new trades only. Existing positions keep being monitored, marked to market, and their stop-loss/take-profit keep being tracked. Auto-flatten is an opt-in config, off by default. |
| 3 | Every rejection is logged with an exact `RejectionReason` enum value — never a free-text guess. |
| 4 | Kelly sizing is always fractional (config multiplier on the raw formula), gated by the same minimum-sample-size rule as `ConfidenceEngine`, and falls back to fixed-fractional sizing when ungated. |
| 5 | Correlation management ships in two phases: Phase A (now) tracks net directional exposure across strategies on the *same* symbol; Phase B (real pairwise correlation across symbols) waits for multi-symbol execution to actually exist. |
| 6 | Kill switch state is persisted to Postgres, not held only in memory — a process restart must never silently clear an emergency stop. Circuit breaker state is in-memory per process (auto-recovering by nature), with every trip/clear transition still logged for audit. |

## 2. Architectural deviations from the original design (read this before touching code)

**`PositionSizer.size()` is a breaking interface change.** It currently
takes `(signal, equity, feature_window)` and returns a bare `float`. A
real Risk Engine cannot enforce portfolio-level exposure, drawdown, or
correlation limits while blind to every other open position — a scalar
equity number isn't enough context. New signature:

```
size(self, signal: Signal, context: RiskContext) -> SizingDecision
```

Every existing implementer of `PositionSizer` (`FixedFractionSizer`,
plus `BacktestEngine`'s call site) must be updated. This is deliberate
and unavoidable, not an oversight — flagging it loudly per rule 9
before any code changes, since it touches an already-built,
already-tested component.

**`Portfolio` gains a read-only snapshot method**, `Portfolio.snapshot()
-> PortfolioView`, rather than exposing its mutable internals directly
to the Risk Engine. The Risk Engine reads a `PortfolioView`; it never
calls `open_position`/`close_position` itself — `BacktestEngine` still
owns all portfolio mutation, preserving the separation of concerns
already established.

**`ExperimentConfig` gains a `risk_config_id` field.** Risk parameters
are now versioned and comparable across experiments exactly like
strategy versions already are.

## 3. Folder structure

```
core/risk/
    __init__.py
    rejection_reason.py          # RejectionReason, ThrottleReason enums
    risk_context.py               # RiskContext dataclass
    risk_decision.py               # SizingDecision, LayerResult dataclasses
    risk_config.py                  # RiskConfig dataclass + from_yaml loader
    kill_switch.py                    # KillSwitch
    circuit_breaker.py                 # CircuitBreaker
    loss_limit_tracker.py               # LossLimitTracker
    drawdown_monitor.py                  # DrawdownMonitor
    exposure_tracker.py                   # ExposureTracker
    position_sizing_strategies.py          # PositionSizingStrategy interface, VolatilityAdjustedSizer, FractionalKellySizer
    risk_engine.py                           # RiskEngine orchestrator
    events.py                                 # risk-specific event dataclasses

config/
    risk_engine.yaml               # NEW — default risk config, mirrors regime_detector.yaml

tests/test_risk/
    __init__.py
    test_rejection_reason.py
    test_kill_switch.py
    test_circuit_breaker.py
    test_loss_limit_tracker.py
    test_drawdown_monitor.py
    test_exposure_tracker.py
    test_position_sizing_strategies.py
    test_portfolio_view.py
    test_risk_engine_integration.py
    test_backtest_engine_risk_integration.py

# MODIFIED existing files
core/position_sizing.py          # widen PositionSizer interface, update FixedFractionSizer
core/backtest_engine.py          # build RiskContext per signal, call widened interface, log risk decisions
core/portfolio.py                # add PortfolioView, PositionView, Portfolio.snapshot()
core/experiment.py               # ExperimentConfig gains risk_config_id
schema.sql                       # new tables, ALTER experiments
CLAUDE.md                        # updated after implementation
```

## 4. Every class, interface, dataclass, enum

### `core/risk/rejection_reason.py`

```
class RejectionReason(Enum):
    KILL_SWITCH_ACTIVE
    CIRCUIT_BREAKER_ACTIVE
    MAX_DAILY_LOSS_REACHED
    MAX_WEEKLY_LOSS_REACHED
    MAX_DRAWDOWN_REACHED
    DATA_QUALITY_FAILED
    EXPOSURE_LIMIT_EXCEEDED
    MAX_OPEN_POSITIONS
    CORRELATION_LIMIT
    POSITION_SIZE_TOO_SMALL
    INSUFFICIENT_SAMPLE_FOR_KELLY

class ThrottleReason(Enum):
    """Distinct from RejectionReason: a throttle REDUCES size, it never
    vetoes the trade to zero on its own."""
    DRAWDOWN_TIER_REDUCTION
    LOW_KELLY_CONFIDENCE
    ELEVATED_VOLATILITY
```

### `core/risk/risk_context.py`

```
@dataclass
class RiskContext:
    equity: float
    feature_window: FeatureWindow
    regime_state: RegimeState
    portfolio_view: PortfolioView
    data_quality_ok: bool
    data_quality_reason: Optional[str]
    as_of: datetime
```

### `core/risk/risk_decision.py`

```
@dataclass
class LayerResult:
    layer_name: str
    passed: bool
    multiplier: float               # 1.0 if this layer applied no throttle
    reason: Optional[str]

@dataclass
class SizingDecision:
    approved_quantity: float        # 0.0 if fully vetoed
    proposed_quantity: float        # pre-cap quantity from the sizing method, for audit
    rejection_reason: Optional[RejectionReason]
    throttle_reasons: list[ThrottleReason]
    layer_results: list[LayerResult]
```

### `core/risk/risk_config.py`

```
@dataclass
class RiskConfig:
    risk_config_id: str
    version: str
    daily_loss_limit_pct: float
    weekly_loss_limit_pct: float
    drawdown_tier_1_pct: float          # throttle threshold
    drawdown_tier_1_factor: float       # e.g. 0.5 -> half size
    drawdown_tier_2_pct: float          # hard stop on new entries
    drawdown_tier_3_pct: float          # auto kill switch threshold
    max_gross_exposure_pct: float
    max_net_exposure_pct: float
    max_concurrent_positions: int
    max_same_symbol_directional_exposure_pct: float
    sizing_method: str                  # 'fixed_fraction' | 'volatility_adjusted' | 'fractional_kelly'
    kelly_fraction_multiplier: float
    kelly_min_sample_size: int
    circuit_breaker_atr_percentile_threshold: float
    circuit_breaker_confirmation_bars: int
    kill_switch_auto_flatten: bool

    @classmethod
    def from_yaml(cls, path: str) -> "RiskConfig": ...
```

### `core/risk/kill_switch.py`

```
class KillSwitch:
    """Persisted to Postgres (kill_switch_state table) — must survive
    process restarts. Auto-engage triggers: drawdown_tier_3 breach, or
    N circuit breaker trips within a short window (systemic-issue
    signal, not a one-off). NEVER auto-clears — engage() and
    disengage() are both explicit, disengage() always logged with who/why."""

    def __init__(self, db_session, scope: str = "global"): ...
    def is_engaged(self) -> bool: ...
    def engage(self, reason: str, engaged_by: str) -> None: ...
    def disengage(self, disengaged_by: str) -> None:
        """Manual re-arm only. No code path calls this automatically."""
    def load_state(self) -> None:
        """Read current state from kill_switch_state on startup —
        this is what makes persistence actually matter."""
```

### `core/risk/circuit_breaker.py`

```
class CircuitBreaker:
    """In-memory per process; reuses the exact confirmation-bar
    hysteresis pattern already built and tested in RegimeDetector —
    N consecutive bars confirming 'condition cleared' before actually
    clearing, not a single clean reading."""

    def __init__(self, name: str, threshold: float, confirmation_bars: int): ...
    def evaluate(self, current_value: float) -> bool:
        """Returns True if tripped (after hysteresis-confirmed)."""
    def reset(self) -> None: ...
```

### `core/risk/loss_limit_tracker.py`

```
class LossLimitTracker:
    """UTC daily (00:00-23:59) and weekly (Mon 00:00 - Sun 23:59)
    realized+unrealized PnL tracking, computed from PortfolioView's
    trade history and open positions — not a separate ledger."""

    def __init__(self, daily_limit_pct: float, weekly_limit_pct: float): ...
    def evaluate(self, portfolio_view: PortfolioView, as_of: datetime) -> tuple[bool, bool]:
        """Returns (daily_breached, weekly_breached)."""
```

### `core/risk/drawdown_monitor.py`

```
class DrawdownMonitor:
    """Tiered response computed from PortfolioView's running peak
    equity — same math as the backtest max_drawdown metric, but
    incremental/live instead of after-the-fact."""

    def __init__(self, tier_1_pct, tier_1_factor, tier_2_pct, tier_3_pct): ...
    def evaluate(self, portfolio_view: PortfolioView) -> "DrawdownTierResult":
        """Returns which tier is currently active and the size
        multiplier (1.0, tier_1_factor, or 0.0 for tier_2+)."""

@dataclass
class DrawdownTierResult:
    tier: int                # 0 = normal, 1 = throttle, 2 = hard stop, 3 = kill-switch-triggering
    current_drawdown_pct: float
    size_multiplier: float
```

### `core/risk/exposure_tracker.py`

```
class ExposureTracker:
    """Phase A: same-symbol directional exposure across strategies.
    Two strategies long the same symbol count against ONE net exposure
    figure, not as independent, uncorrelated bets."""

    def __init__(self, max_gross_pct, max_net_pct, max_concurrent, max_same_symbol_directional_pct): ...
    def evaluate(self, portfolio_view: PortfolioView, proposed_direction: int) -> "ExposureResult"

@dataclass
class ExposureResult:
    within_limits: bool
    reason: Optional[RejectionReason]
    gross_exposure_pct: float
    net_exposure_pct: float
```

### `core/risk/position_sizing_strategies.py`

```
class PositionSizingStrategy(ABC):
    """Internal to RiskEngine — NOT the same interface BacktestEngine
    calls. This computes only the base quantity; RiskEngine applies
    every throttle/cap on top of it."""

    @abstractmethod
    def compute_base_quantity(self, signal: Signal, context: RiskContext) -> tuple[float, Optional[RejectionReason]]: ...


class VolatilityAdjustedSizer(PositionSizingStrategy):
    """risk_amount = equity * risk_fraction. stop_distance = strategy's
    own stop if set, else k * atr_14 from context.feature_window.
    quantity = risk_amount / stop_distance."""


class FractionalKellySizer(PositionSizingStrategy):
    """f* = (b*p - q) / b, sourced from the SAME regime-conditioned,
    sample-size-gated historical stats ConfidenceEngine already
    computes — this class does not maintain its own separate
    performance history. Multiplies f* by config.kelly_fraction_multiplier.
    Returns (0.0, INSUFFICIENT_SAMPLE_FOR_KELLY) if sample_size is
    below config.kelly_min_sample_size — never guesses with thin data."""
```

### `core/risk/risk_engine.py`

```
class RiskEngine(PositionSizer):
    """Implements the widened PositionSizer interface. Orchestrates the
    five-layer pipeline in strict, fail-fast order:
      1. gate layer    — kill switch, circuit breakers, data quality
      2. budget layer  — daily/weekly loss limits, drawdown tier
      3. portfolio layer — exposure + same-symbol correlation
      4. sizing layer  — base quantity via configured PositionSizingStrategy,
                          scaled by every throttle multiplier from layers 2-3
      5. decision layer — hard per-trade cap, produce + log SizingDecision
    """

    def __init__(
        self, config: RiskConfig, kill_switch: KillSwitch,
        circuit_breakers: list[CircuitBreaker], loss_limit_tracker: LossLimitTracker,
        drawdown_monitor: DrawdownMonitor, exposure_tracker: ExposureTracker,
        sizing_strategy: PositionSizingStrategy, event_bus: EventBus, db_session,
    ): ...

    def size(self, signal: Signal, context: RiskContext) -> SizingDecision: ...
```

### `core/risk/events.py`

```
@dataclass
class RiskDecisionMade:
    experiment_id: Optional[int]; strategy_id: str; bar_time: datetime
    approved_quantity: float; rejection_reason: Optional[str]

@dataclass
class CircuitBreakerTripped:
    breaker_name: str; reason: str; occurred_at: datetime

@dataclass
class CircuitBreakerCleared:
    breaker_name: str; occurred_at: datetime

@dataclass
class KillSwitchEngaged:
    engaged_by: str; reason: str; occurred_at: datetime

@dataclass
class KillSwitchDisengaged:
    disengaged_by: str; occurred_at: datetime

@dataclass
class DailyLossLimitBreached:
    date: date; realized_pnl_pct: float; occurred_at: datetime

@dataclass
class DrawdownTierChanged:
    previous_tier: int; new_tier: int; current_drawdown_pct: float; occurred_at: datetime
```

### `core/portfolio.py` — additions

```
@dataclass
class PositionView:
    strategy_id: str; direction: int; entry_price: float
    quantity: float; unrealized_pnl: float

@dataclass
class PortfolioView:
    equity: float
    peak_equity: float
    open_positions: list[PositionView]
    trade_history: list[Trade]          # for LossLimitTracker's UTC-window filtering

class Portfolio:
    def snapshot(self, current_price: float) -> PortfolioView:
        """Read-only. The Risk Engine's only window into Portfolio state."""
```

### `core/position_sizing.py` — modified

```
class PositionSizer(ABC):
    @abstractmethod
    def size(self, signal: Signal, context: RiskContext) -> SizingDecision: ...
        # BREAKING CHANGE from (signal, equity, feature_window) -> float

class FixedFractionSizer(PositionSizer):
    # updated to the new signature; wraps its single float answer in a
    # SizingDecision with empty layer_results — remains the deliberately
    # naive baseline for comparison experiments, not deprecated.
```

## 5. Database schema changes

```sql
CREATE TABLE risk_config (
    risk_config_id      TEXT PRIMARY KEY,
    version              TEXT NOT NULL,
    daily_loss_limit_pct  NUMERIC,
    weekly_loss_limit_pct NUMERIC,
    drawdown_tier_1_pct   NUMERIC,
    drawdown_tier_1_factor NUMERIC,
    drawdown_tier_2_pct   NUMERIC,
    drawdown_tier_3_pct   NUMERIC,
    max_gross_exposure_pct NUMERIC,
    max_net_exposure_pct   NUMERIC,
    max_concurrent_positions INT,
    max_same_symbol_directional_exposure_pct NUMERIC,
    sizing_method          TEXT,
    kelly_fraction_multiplier NUMERIC,
    kelly_min_sample_size    INT,
    circuit_breaker_atr_percentile_threshold NUMERIC,
    circuit_breaker_confirmation_bars INT,
    kill_switch_auto_flatten BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE risk_decision_log (
    id                BIGSERIAL PRIMARY KEY,
    experiment_id     INT REFERENCES experiments(experiment_id),
    bar_time          TIMESTAMPTZ NOT NULL,
    strategy_id       TEXT NOT NULL,
    proposed_quantity NUMERIC,
    approved_quantity NUMERIC,
    rejection_reason  TEXT,           -- RejectionReason value, or null
    throttle_reasons  TEXT[],
    layer_results     JSONB,
    risk_config_id    TEXT REFERENCES risk_config(risk_config_id)
);

CREATE TABLE kill_switch_state (
    scope                  TEXT PRIMARY KEY,     -- 'global' for V1
    engaged                BOOLEAN NOT NULL DEFAULT FALSE,
    engaged_at             TIMESTAMPTZ,
    engaged_reason         TEXT,
    engaged_by             TEXT,
    auto_flatten_positions BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at             TIMESTAMPTZ NOT NULL
);

CREATE TABLE circuit_breaker_event_log (
    id            BIGSERIAL PRIMARY KEY,
    breaker_name  TEXT NOT NULL,
    event_type    TEXT NOT NULL,   -- 'tripped' | 'cleared'
    reason        TEXT,
    occurred_at   TIMESTAMPTZ NOT NULL
);

ALTER TABLE experiments ADD COLUMN risk_config_id TEXT REFERENCES risk_config(risk_config_id);
```

## 6. Testing strategy

- Each of `KillSwitch`, `CircuitBreaker`, `LossLimitTracker`,
  `DrawdownMonitor`, `ExposureTracker` gets its own test file, tested
  in total isolation from `RiskEngine`.
- `LossLimitTracker`: explicit test at the UTC midnight boundary itself
  (23:59:59 vs 00:00:00), not just "24 hours apart" — this is exactly
  where an off-by-one would hide.
- `CircuitBreaker`: trip, confirm it doesn't clear on one clean reading,
  confirm it does clear after `confirmation_bars` consecutive clean
  readings — mirrors `test_regime_detector.py`'s hysteresis tests
  almost exactly; reuse that structure.
- `KillSwitch`: confirm state survives being reconstructed from the DB
  (simulating a process restart) — this is the whole point of
  persisting it; a test that only checks in-memory behavior would miss
  the actual requirement.
- `FractionalKellySizer`: hand-computed expected values for known
  (p, b) pairs; explicit test that insufficient sample size returns
  `INSUFFICIENT_SAMPLE_FOR_KELLY` and quantity 0, never a guess.
- `test_risk_engine_integration.py`: full five-layer pipeline against a
  constructed `RiskContext`/`PortfolioView`, asserting the exact
  `RejectionReason` for each failure mode — one test per enum value.
  Include property-style tests: run many randomized signals through
  the engine and assert invariants that must always hold (approved
  quantity never exceeds the configured hard cap; nothing is ever
  approved while `KillSwitch.is_engaged()` is true).
- `test_backtest_engine_risk_integration.py`: wire `RiskEngine` in as
  `BacktestEngine`'s `position_sizer` and run an actual backtest,
  confirming trades get sized/rejected as expected end-to-end.
- **Re-run the full existing suite (62 tests) after the `PositionSizer`
  interface change** — this is a breaking change to an already-tested
  component; passing the old suite unmodified except for the
  interface-conforming updates is the acceptance bar, not optional.

## 7. Step-by-step build order

Each step should be implemented, tested, and reviewed before the next
— matching how every prior component in this project was built.

1. Foundational types: `RejectionReason`, `ThrottleReason`,
   `RiskContext`, `SizingDecision`, `LayerResult`, `PositionView`,
   `PortfolioView`, `Portfolio.snapshot()`. No behavior yet — pure data
   shapes and their tests.
2. `RiskConfig` + YAML loader + `risk_config` table + `ExperimentConfig.risk_config_id`.
3. `KillSwitch`, with Postgres persistence + `kill_switch_state` table + tests including the restart-survival test.
4. `CircuitBreaker` + `circuit_breaker_event_log` + tests (hysteresis, reusing the regime detector's pattern).
5. `LossLimitTracker` + tests (UTC boundary explicit).
6. `DrawdownMonitor` + tests (tier transitions).
7. `ExposureTracker` + tests (Phase A same-symbol directional exposure).
8. `PositionSizingStrategy` interface + `VolatilityAdjustedSizer` + `FractionalKellySizer` + tests.
9. `RiskEngine` orchestrator wiring 1-8 through the five layers, `risk_decision_log` persistence, event publishing + integration/property tests.
10. Widen `PositionSizer`, update `FixedFractionSizer`, update `BacktestEngine` to build `RiskContext` and log `SizingDecision`s — then re-run and confirm the full existing suite still passes.
11. Update `CLAUDE.md`.

## 8. Definition of done

- All new tests passing, shown, not assumed.
- Full previously-existing test suite still green after the interface change.
- A real backtest run with `RiskEngine` as the `position_sizer`,
  producing a populated `risk_decision_log` with a mix of approvals,
  throttles, and at least one of each `RejectionReason` exercised in
  tests.
- `CLAUDE.md` updated to reflect what was built, per the established pattern.
