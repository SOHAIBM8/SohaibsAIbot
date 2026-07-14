"""
Risk monitoring API (spec section 14/26). Read-only endpoints only,
until this docstring's own control-surface note below — kill-switch
engage/disengage, arm/disarm route through the exact same
KillSwitch/ArmingService methods core/risk_engine.py and
core/security/arming_service.py already enforce; this layer adds
authentication, CSRF, and the confirmation-dialog friction the
frontend enforces, never a relaxation of policy (spec section 4).

GET /api/risk/arming wraps ArmingService.get() (spec section 26 maps
ArmingService to the "Risk" row, not a separate "Strategies" row —
there is no such row in the spec's own integration-points table, and
building a full Strategy Management page (spec section 13) needs a
cached/singleton StrategyRegistry instance and real design work beyond
a read wrapper; flagged as a known gap rather than rushed).

Note on "current drawdown tier" / exposure / loss-limit state: those
three calculators (DrawdownMonitor, ExposureTracker, LossLimitTracker)
are pure, stateless, config-driven functions that take a live
PortfolioView per call — see their own module docstrings. They hold no
state of their own and this API process has no live PortfolioView to
hand them (nothing in the codebase persists a point-in-time portfolio
snapshot yet; see CLAUDE.md's "account_snapshots" known gap). The most
recent risk_decision_log row is the one real, persisted trace of what
those layers actually decided the last time RiskEngine ran — exposed
via /api/risk/decisions rather than faked by recomputing a number this
process cannot correctly compute.

Control surfaces (spec decision #2, section 26's kill-switch/arming
rows), built together as one step per the confirmed control-surface
scope decision:

- POST /kill-switch/engage, /kill-switch/disengage — KillSwitch.engage()/
  disengage() have no authorization logic of their own (confirmed by
  research before writing this); this route IS the authorization
  boundary, via get_current_session (CSRF + valid session required for
  every mutating call, same as every other POST/PUT in this project).
- POST /arming/arm, /arming/disarm — wrap ArmingService.arm()/disarm().
  `mainnet=True` is REJECTED outright (400), not passed through:
  MainnetGate (core/security/mainnet_gate.py) already forbids pairing
  a mainnet credential with the dev-only LocalDevKMSClient anywhere in
  this system, and this dashboard build has no real cloud KMS
  configured (CLAUDE.md's own confirmed scope limit) — arming a
  mainnet strategy from here would create consent for a live-money
  action this deployment cannot safely execute. Testnet/paper arming
  only, until a real KMS exists.
- `engaged_by`/`armed_by`/`disarmed_by` is the authenticated operator's
  own username (DashboardSettings.operator_username) — single-operator
  V1 (spec decision #7), so there is no other real identity to
  attribute the action to.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from api.auth.dependencies import get_current_session, get_settings
from api.auth.session_store import DashboardSession
from api.config import DashboardSettings
from api.db import get_db
from api.schemas.risk import (
    ArmingStateOut,
    ArmRequestIn,
    CircuitBreakerStateOut,
    DisarmRequestIn,
    KillSwitchEngageIn,
    KillSwitchStateOut,
    LayerResultOut,
    RiskConfigOut,
    RiskDecisionRecordOut,
)
from core.risk.circuit_breaker import get_current_circuit_breaker_states
from core.risk.kill_switch import KillSwitch
from core.risk.risk_config import RiskConfig
from core.risk.risk_decision import RiskDecisionLogReader
from core.security.arming_service import ArmingService

router = APIRouter(prefix="/api/risk", tags=["risk"])

RISK_CONFIG_PATH = "config/risk_engine.yaml"


@router.get("/config", response_model=RiskConfigOut)
def get_risk_config(
    _session: DashboardSession = Depends(get_current_session),
) -> RiskConfigOut:
    config = RiskConfig.from_yaml(RISK_CONFIG_PATH)
    return RiskConfigOut.model_validate(config)


@router.get("/kill-switch", response_model=KillSwitchStateOut)
def get_kill_switch_state(
    db: Session = Depends(get_db),
    _session: DashboardSession = Depends(get_current_session),
) -> KillSwitchStateOut:
    switch = KillSwitch(db, scope="global")
    return KillSwitchStateOut.model_validate(switch.get_state())


@router.post("/kill-switch/engage", response_model=KillSwitchStateOut)
def engage_kill_switch(
    body: KillSwitchEngageIn,
    db: Session = Depends(get_db),
    settings: DashboardSettings = Depends(get_settings),
    _session: DashboardSession = Depends(get_current_session),
) -> KillSwitchStateOut:
    switch = KillSwitch(db, scope="global")
    switch.engage(reason=body.reason, engaged_by=settings.operator_username)
    return KillSwitchStateOut.model_validate(switch.get_state())


@router.post("/kill-switch/disengage", response_model=KillSwitchStateOut)
def disengage_kill_switch(
    db: Session = Depends(get_db),
    settings: DashboardSettings = Depends(get_settings),
    _session: DashboardSession = Depends(get_current_session),
) -> KillSwitchStateOut:
    switch = KillSwitch(db, scope="global")
    switch.disengage(disengaged_by=settings.operator_username)
    return KillSwitchStateOut.model_validate(switch.get_state())


@router.get("/circuit-breakers", response_model=list[CircuitBreakerStateOut])
def get_circuit_breaker_states(
    db: Session = Depends(get_db),
    _session: DashboardSession = Depends(get_current_session),
) -> list[CircuitBreakerStateOut]:
    states = get_current_circuit_breaker_states(db)
    return [CircuitBreakerStateOut.model_validate(s) for s in states]


@router.get("/arming", response_model=ArmingStateOut)
def get_arming_state(
    strategy_id: str = Query(...),
    exchange: str = Query(...),
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> ArmingStateOut:
    service = ArmingService(db)
    state = service.get(session.account_id, strategy_id, exchange)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no arming record for this account/strategy/exchange",
        )
    return ArmingStateOut.model_validate(state)


@router.post("/arming/arm", response_model=ArmingStateOut)
def arm_strategy(
    body: ArmRequestIn,
    db: Session = Depends(get_db),
    settings: DashboardSettings = Depends(get_settings),
    session: DashboardSession = Depends(get_current_session),
) -> ArmingStateOut:
    if body.mainnet:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "mainnet arming is not permitted from this dashboard build — no real cloud "
                "KMS is configured (see CLAUDE.md's confirmed scope limit); MainnetGate "
                "forbids pairing a mainnet credential with the dev-only KMS this project uses."
            ),
        )
    service = ArmingService(db)
    service.arm(
        account_id=session.account_id,
        strategy_id=body.strategy_id,
        exchange=body.exchange,
        armed_by=settings.operator_username,
        mainnet=False,
    )
    state = service.get(session.account_id, body.strategy_id, body.exchange)
    assert state is not None  # arm() just wrote this row
    return ArmingStateOut.model_validate(state)


@router.post("/arming/disarm", response_model=ArmingStateOut)
def disarm_strategy(
    body: DisarmRequestIn,
    db: Session = Depends(get_db),
    session: DashboardSession = Depends(get_current_session),
) -> ArmingStateOut:
    service = ArmingService(db)
    service.disarm(
        account_id=session.account_id,
        strategy_id=body.strategy_id,
        exchange=body.exchange,
        reason=body.reason,
    )
    state = service.get(session.account_id, body.strategy_id, body.exchange)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no arming record for this account/strategy/exchange",
        )
    return ArmingStateOut.model_validate(state)


@router.get("/decisions", response_model=list[RiskDecisionRecordOut])
def get_recent_decisions(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    _session: DashboardSession = Depends(get_current_session),
) -> list[RiskDecisionRecordOut]:
    reader = RiskDecisionLogReader(db)
    records = reader.list_recent(limit=limit)
    return [
        RiskDecisionRecordOut(
            id=r.id,
            experiment_id=r.experiment_id,
            bar_time=r.bar_time,
            strategy_id=r.strategy_id,
            proposed_quantity=r.proposed_quantity,
            approved_quantity=r.approved_quantity,
            rejection_reason=r.rejection_reason.value if r.rejection_reason else None,
            throttle_reasons=[t.value for t in r.throttle_reasons],
            layer_results=[LayerResultOut.model_validate(lr) for lr in r.layer_results],
            risk_config_id=r.risk_config_id,
        )
        for r in records
    ]
