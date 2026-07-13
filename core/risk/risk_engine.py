"""
RiskEngine: orchestrates the five-layer pipeline in strict, fail-fast
order (spec section 4, core/risk/risk_engine.py):
  1. gate layer      — kill switch, circuit breakers, data quality
  2. budget layer     — daily/weekly loss limits, drawdown tier
  3. portfolio layer   — exposure + same-symbol correlation
  4. sizing layer       — base quantity via the configured
                          PositionSizingStrategy, scaled by every
                          throttle multiplier from layers 2-3
  5. decision layer      — hard per-trade cap, produce + log SizingDecision

Design notes (rule 9 — gaps in the spec, filled in and flagged here):

1. `class RiskEngine(PositionSizer)`: added in step 10, once
   PositionSizer.size() was widened to `(signal, context) ->
   SizingDecision` to actually match this class's signature — see
   core/position_sizing.py. Before that widening landed, subclassing
   PositionSizer here would have meant overriding an abstract method
   with an incompatible signature, a real Liskov violation.

2. Circuit breakers all read the SAME signal: `atr_percentile_90` from
   the feature window. RiskConfig only ever configures one circuit-
   breaker dimension (`circuit_breaker_atr_percentile_threshold` /
   `circuit_breaker_confirmation_bars`, both singular) — there's no
   config surface for a breaker watching anything else, so every
   CircuitBreaker instance passed in is fed that one reading.

3. The spec's KillSwitch docstring lists two auto-engage triggers:
   "drawdown_tier_3 breach, or N circuit breaker trips within a short
   window." Only the first is implemented — the second has no N, no
   window, and no RiskConfig field defining either, anywhere in the
   spec. Rather than invent arbitrary numbers, this trigger is left
   unimplemented; flagging it explicitly so it isn't mistaken for an
   oversight.

4. "Hard per-trade cap" (layer 5) has no dedicated RiskConfig field.
   Reusing `max_same_symbol_directional_exposure_pct` as a notional
   ceiling per trade (equity * pct / entry_price) — the only exposure-
   percentage field already scoped to "one symbol, one direction,"
   which is exactly what a single trade is. This satisfies the
   integration test invariant the spec itself calls for: approved
   quantity never exceeds this configured cap.

5. `risk_decision_log.risk_config_id` is a FK into `risk_config`, but
   nothing in the spec ever inserts a `risk_config` row — RiskEngine's
   constructor upserts `config` into `risk_config` on construction
   (`core.risk.risk_config.upsert_risk_config`) so that FK is always
   satisfiable. See the note in risk_config.py step 2 left this
   exact gap open; this is where it gets closed.

6. RiskEngine's constructor gains one param beyond the spec's literal
   list: `experiment_id: int | None = None`. `RiskDecisionMade` and
   `risk_decision_log` both need it, but `size(signal, context)` has
   no room to pass one in per-call — it's set once at construction,
   consistent with one RiskEngine instance living for one backtest run.
"""

import json
from dataclasses import asdict

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.ingestion.event_bus import EventBus
from core.position_sizing import PositionSizer
from core.risk.circuit_breaker import CircuitBreaker, record_circuit_breaker_event
from core.risk.drawdown_monitor import DrawdownMonitor
from core.risk.events import (
    CircuitBreakerCleared,
    CircuitBreakerTripped,
    DailyLossLimitBreached,
    DrawdownTierChanged,
    KillSwitchEngaged,
    RiskDecisionMade,
)
from core.risk.exposure_tracker import ExposureTracker
from core.risk.kill_switch import KillSwitch
from core.risk.loss_limit_tracker import LossLimitTracker
from core.risk.position_sizing_strategies import PositionSizingStrategy
from core.risk.rejection_reason import RejectionReason, ThrottleReason
from core.risk.risk_config import RiskConfig, upsert_risk_config
from core.risk.risk_context import RiskContext
from core.risk.risk_decision import LayerResult, SizingDecision
from core.strategy_base import Signal


class RiskEngine(PositionSizer):
    def __init__(
        self,
        config: RiskConfig,
        kill_switch: KillSwitch,
        circuit_breakers: list[CircuitBreaker],
        loss_limit_tracker: LossLimitTracker,
        drawdown_monitor: DrawdownMonitor,
        exposure_tracker: ExposureTracker,
        sizing_strategy: PositionSizingStrategy,
        event_bus: EventBus,
        db_session: Session,
        experiment_id: int | None = None,
    ):
        self.config = config
        self.kill_switch = kill_switch
        self.circuit_breakers = circuit_breakers
        self.loss_limit_tracker = loss_limit_tracker
        self.drawdown_monitor = drawdown_monitor
        self.exposure_tracker = exposure_tracker
        self.sizing_strategy = sizing_strategy
        self.event_bus = event_bus
        self.db = db_session
        self.experiment_id = experiment_id
        self._last_drawdown_tier = 0
        self._daily_breach_active = False

        upsert_risk_config(self.db, self.config)

    def size(self, signal: Signal, context: RiskContext) -> SizingDecision:
        layer_results: list[LayerResult] = []
        throttle_reasons: list[ThrottleReason] = []

        gate_reason = self._evaluate_gate(context, layer_results)
        if gate_reason is not None:
            return self._finalize(
                signal, context, 0.0, 0.0, gate_reason, throttle_reasons, layer_results
            )

        budget_reason, budget_multiplier = self._evaluate_budget(
            context, layer_results, throttle_reasons
        )
        if budget_reason is not None:
            return self._finalize(
                signal, context, 0.0, 0.0, budget_reason, throttle_reasons, layer_results
            )

        portfolio_reason = self._evaluate_portfolio(signal, context, layer_results)
        if portfolio_reason is not None:
            return self._finalize(
                signal, context, 0.0, 0.0, portfolio_reason, throttle_reasons, layer_results
            )

        proposed_quantity, sizing_reason = self.sizing_strategy.compute_base_quantity(
            signal, context
        )
        if sizing_reason is not None:
            layer_results.append(LayerResult("sizing", False, 0.0, sizing_reason.value))
            return self._finalize(
                signal,
                context,
                0.0,
                proposed_quantity,
                sizing_reason,
                throttle_reasons,
                layer_results,
            )
        sized_quantity = proposed_quantity * budget_multiplier
        layer_results.append(
            LayerResult(
                "sizing",
                True,
                budget_multiplier,
                "drawdown throttle applied" if budget_multiplier < 1.0 else None,
            )
        )

        approved_quantity = self._apply_hard_cap(sized_quantity, signal, context)
        decision_multiplier = (approved_quantity / sized_quantity) if sized_quantity > 0 else 1.0
        final_reason = None
        if approved_quantity <= 0:
            final_reason = RejectionReason.POSITION_SIZE_TOO_SMALL
            layer_results.append(LayerResult("decision", False, 0.0, "capped quantity is zero"))
        else:
            layer_results.append(
                LayerResult(
                    "decision",
                    True,
                    decision_multiplier,
                    "hard per-trade cap applied" if decision_multiplier < 1.0 else None,
                )
            )

        return self._finalize(
            signal,
            context,
            approved_quantity,
            proposed_quantity,
            final_reason,
            throttle_reasons,
            layer_results,
        )

    # --- layers ------------------------------------------------------

    def _evaluate_gate(
        self, context: RiskContext, layer_results: list[LayerResult]
    ) -> RejectionReason | None:
        if self.kill_switch.is_engaged():
            layer_results.append(LayerResult("gate", False, 0.0, "kill switch engaged"))
            return RejectionReason.KILL_SWITCH_ACTIVE

        try:
            current_value: float | None = context.feature_window.get("atr_percentile_90")
        except KeyError:
            current_value = None

        if current_value is not None:
            for breaker in self.circuit_breakers:
                evaluation = breaker.evaluate_detailed(current_value)
                if evaluation.transitioned and evaluation.event_type is not None:
                    record_circuit_breaker_event(
                        self.db, breaker.name, evaluation.event_type, evaluation.reason or ""
                    )
                    if evaluation.event_type == "tripped":
                        self.event_bus.publish(
                            CircuitBreakerTripped(
                                breaker_name=breaker.name,
                                reason=evaluation.reason or "",
                                occurred_at=context.as_of,
                            )
                        )
                    else:
                        self.event_bus.publish(
                            CircuitBreakerCleared(
                                breaker_name=breaker.name, occurred_at=context.as_of
                            )
                        )
                if evaluation.tripped:
                    layer_results.append(
                        LayerResult("gate", False, 0.0, f"circuit breaker {breaker.name} tripped")
                    )
                    return RejectionReason.CIRCUIT_BREAKER_ACTIVE

        if not context.data_quality_ok:
            layer_results.append(
                LayerResult(
                    "gate", False, 0.0, context.data_quality_reason or "data quality check failed"
                )
            )
            return RejectionReason.DATA_QUALITY_FAILED

        layer_results.append(LayerResult("gate", True, 1.0, None))
        return None

    def _evaluate_budget(
        self,
        context: RiskContext,
        layer_results: list[LayerResult],
        throttle_reasons: list[ThrottleReason],
    ) -> tuple[RejectionReason | None, float]:
        daily_breached, weekly_breached = self.loss_limit_tracker.evaluate(
            context.portfolio_view, context.as_of
        )

        if daily_breached:
            if not self._daily_breach_active:
                self.event_bus.publish(
                    DailyLossLimitBreached(date=context.as_of.date(), occurred_at=context.as_of)
                )
            self._daily_breach_active = True
            layer_results.append(LayerResult("budget", False, 0.0, "daily loss limit reached"))
            return RejectionReason.MAX_DAILY_LOSS_REACHED, 0.0
        self._daily_breach_active = False

        if weekly_breached:
            layer_results.append(LayerResult("budget", False, 0.0, "weekly loss limit reached"))
            return RejectionReason.MAX_WEEKLY_LOSS_REACHED, 0.0

        drawdown_result = self.drawdown_monitor.evaluate(context.portfolio_view)
        if drawdown_result.tier != self._last_drawdown_tier:
            self.event_bus.publish(
                DrawdownTierChanged(
                    previous_tier=self._last_drawdown_tier,
                    new_tier=drawdown_result.tier,
                    current_drawdown_pct=drawdown_result.current_drawdown_pct,
                    occurred_at=context.as_of,
                )
            )
            self._last_drawdown_tier = drawdown_result.tier

        if drawdown_result.tier == 3:
            if not self.kill_switch.is_engaged():
                reason = f"drawdown tier 3 breach ({drawdown_result.current_drawdown_pct:.2%})"
                self.kill_switch.engage(reason=reason, engaged_by="risk_engine")
                self.event_bus.publish(
                    KillSwitchEngaged(
                        engaged_by="risk_engine", reason=reason, occurred_at=context.as_of
                    )
                )
            layer_results.append(
                LayerResult("budget", False, 0.0, "drawdown tier 3 — kill switch engaged")
            )
            return RejectionReason.MAX_DRAWDOWN_REACHED, 0.0

        if drawdown_result.tier == 2:
            layer_results.append(
                LayerResult("budget", False, 0.0, "drawdown tier 2 — hard stop on new entries")
            )
            return RejectionReason.MAX_DRAWDOWN_REACHED, 0.0

        if drawdown_result.tier == 1:
            throttle_reasons.append(ThrottleReason.DRAWDOWN_TIER_REDUCTION)
            layer_results.append(
                LayerResult(
                    "budget", True, drawdown_result.size_multiplier, "drawdown tier 1 throttle"
                )
            )
            return None, drawdown_result.size_multiplier

        layer_results.append(LayerResult("budget", True, 1.0, None))
        return None, 1.0

    def _evaluate_portfolio(
        self, signal: Signal, context: RiskContext, layer_results: list[LayerResult]
    ) -> RejectionReason | None:
        result = self.exposure_tracker.evaluate(context.portfolio_view, signal.direction)
        if not result.within_limits:
            reason_text = result.reason.value if result.reason else "exposure limit exceeded"
            layer_results.append(LayerResult("portfolio", False, 0.0, reason_text))
            return result.reason
        layer_results.append(LayerResult("portfolio", True, 1.0, None))
        return None

    def _apply_hard_cap(self, quantity: float, signal: Signal, context: RiskContext) -> float:
        if quantity <= 0 or signal.entry_price <= 0 or context.equity <= 0:
            return 0.0
        hard_cap_notional = context.equity * self.config.max_same_symbol_directional_exposure_pct
        hard_cap_quantity = hard_cap_notional / signal.entry_price
        return min(quantity, hard_cap_quantity)

    # --- finalization --------------------------------------------------

    def _finalize(
        self,
        signal: Signal,
        context: RiskContext,
        approved_quantity: float,
        proposed_quantity: float,
        rejection_reason: RejectionReason | None,
        throttle_reasons: list[ThrottleReason],
        layer_results: list[LayerResult],
    ) -> SizingDecision:
        decision = SizingDecision(
            approved_quantity=approved_quantity,
            proposed_quantity=proposed_quantity,
            rejection_reason=rejection_reason,
            throttle_reasons=throttle_reasons,
            layer_results=layer_results,
        )
        self._log_decision(signal, context, decision)
        self.event_bus.publish(
            RiskDecisionMade(
                experiment_id=self.experiment_id,
                strategy_id=signal.strategy_id,
                bar_time=context.as_of,
                approved_quantity=approved_quantity,
                rejection_reason=rejection_reason.value if rejection_reason else None,
            )
        )
        return decision

    def _log_decision(self, signal: Signal, context: RiskContext, decision: SizingDecision) -> None:
        result = self.db.execute(
            text("""
                INSERT INTO risk_decision_log (
                    experiment_id, bar_time, strategy_id, proposed_quantity, approved_quantity,
                    rejection_reason, throttle_reasons, layer_results, risk_config_id
                ) VALUES (
                    :experiment_id, :bar_time, :strategy_id, :proposed_quantity, :approved_quantity,
                    :rejection_reason, :throttle_reasons, :layer_results, :risk_config_id
                )
                RETURNING id
                """),
            {
                "experiment_id": self.experiment_id,
                "bar_time": context.as_of,
                "strategy_id": signal.strategy_id,
                "proposed_quantity": decision.proposed_quantity,
                "approved_quantity": decision.approved_quantity,
                "rejection_reason": (
                    decision.rejection_reason.value if decision.rejection_reason else None
                ),
                "throttle_reasons": [t.value for t in decision.throttle_reasons],
                "layer_results": json.dumps([asdict(lr) for lr in decision.layer_results]),
                "risk_config_id": self.config.risk_config_id,
            },
        )
        decision.risk_decision_id = result.scalar_one()
        self.db.commit()
