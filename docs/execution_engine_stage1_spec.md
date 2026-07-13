# Live Execution & Paper Trading — Stage 1 implementation specification

Status: approved architecture (Stage 1 of 3). Read alongside `CLAUDE.md`
and all prior specs in `docs/`. This spec covers ONLY: the shared order
state machine, the paper trading engine, and read-only market data via
WebSocket. It deliberately excludes real exchange order placement
(Stage 2) and live trading enablement/API key custody (Stage 3) — see
the phase rationale in the architecture discussion above. Do not
implement Stage 2/3 concerns from this spec; they get their own specs
once Stage 1 is reviewed and working.

## 1. Locked-in decisions

| # | Decision |
|---|----------|
| 1 | `OrderManager` and the order state machine are IDENTICAL for paper and live — only the `ExecutionAdapter` implementation differs. |
| 2 | `client_order_id` is generated locally, before any adapter call, and is the sole idempotency key — never a retry counter. |
| 3 | Paper trading reuses the existing `ExecutionModel` (fee/slippage) rather than a second implementation, plus a configurable fixed/jittered latency. |
| 4 | Every order, paper or live, must originate from a `SizingDecision` — no code path places an order without Risk Engine approval. |
| 5 | Stage 1 ships with zero exchange authentication and zero real order placement — `LiveExecutionAdapter` is a stub interface only, unimplemented until Stage 2. |

## 2. Folder structure

```
core/execution/
    __init__.py
    order.py                     # Order dataclass, OrderState enum, OrderType enum
    order_manager.py               # OrderManager, the state machine
    execution_adapter.py             # ExecutionAdapter interface
    paper_execution_adapter.py         # PaperExecutionAdapter (Stage 1 concrete implementation)
    live_execution_adapter.py            # LiveExecutionAdapter (interface stub only — Stage 2 implements)
    fill_simulator.py                      # Paper fill simulation, reusing core/execution_model.py
    latency_simulator.py                     # Configurable fixed/jittered delay
    events.py                                  # execution-specific event dataclasses

core/marketdata/
    __init__.py
    websocket_connection.py       # Connection manager: reconnect, heartbeat
    market_data_normalizer.py       # Raw exchange payload -> normalized event

config/
    execution.yaml                # Stage 1 config: paper account starting balance, latency params

tests/test_execution/
    __init__.py
    test_order_state_machine.py
    test_paper_execution_adapter.py
    test_fill_simulator.py
    test_order_manager_integration.py
tests/test_marketdata/
    __init__.py
    test_websocket_connection.py    # against a fake WebSocket server, no real network
    test_market_data_normalizer.py

# MODIFIED existing files
schema.sql             # new tables: orders, fills, paper_accounts, account_snapshots
CLAUDE.md               # updated after implementation
```

## 3. Every class, interface, dataclass, enum

### `core/execution/order.py`

```
class OrderState(Enum):
    PENDING; SUBMITTED; PARTIALLY_FILLED; FILLED
    PENDING_CANCEL; CANCELLED; REJECTED

class OrderType(Enum):
    MARKET; LIMIT; STOP; STOP_LIMIT; OCO

@dataclass
class Order:
    client_order_id: str          # generated locally, BEFORE any adapter call
    strategy_id: str
    symbol: str
    order_type: OrderType
    direction: int                 # 1 buy, -1 sell
    quantity: float
    limit_price: Optional[float]
    stop_price: Optional[float]
    mode: str                      # 'paper' | 'live'
    state: OrderState
    risk_decision_id: int          # FK to risk_decision_log — required, not optional
    created_at: datetime
    exchange_order_id: Optional[str]   # null until Stage 2 assigns one

@dataclass
class Fill:
    client_order_id: str
    fill_price: float
    quantity: float
    fee: float
    filled_at: datetime
    is_partial: bool
```

### `core/execution/execution_adapter.py`

```
class ExecutionAdapter(ABC):
    """The ONLY thing that differs between paper and live. OrderManager
    is written entirely against this interface and must never branch
    on mode='paper' vs 'live' internally — if it needs to, that's a
    sign logic leaked out of the adapter."""

    @abstractmethod
    def submit_order(self, order: Order) -> Order: ...
    @abstractmethod
    def cancel_order(self, client_order_id: str) -> Order: ...
    @abstractmethod
    def get_order_status(self, client_order_id: str) -> Order: ...
```

### `core/execution/paper_execution_adapter.py`

```
class PaperExecutionAdapter(ExecutionAdapter):
    """Stage 1's concrete implementation. Simulates fills using the
    EXISTING ExecutionModel (core/execution_model.py) — no second
    fee/slippage model — plus a configurable latency delay before the
    simulated fill is recorded."""

    def __init__(self, execution_model: ExecutionModel,
                 latency_simulator: LatencySimulator, market_data_source): ...
```

### `core/execution/live_execution_adapter.py`

```
class LiveExecutionAdapter(ExecutionAdapter):
    """STAGE 2 STUB. Do not implement submit_order/cancel_order logic
    in Stage 1 — this class exists only so OrderManager can be written
    against the full interface now. Every method raises
    NotImplementedError with a message pointing at the Stage 2 spec."""
```

### `core/execution/order_manager.py`

```
class OrderManager:
    """The shared state machine, identical for paper and live. Accepts
    only orders backed by an already-approved SizingDecision."""

    def __init__(self, execution_adapter: ExecutionAdapter, event_bus: EventBus, db_session): ...

    def submit(self, sizing_decision: SizingDecision, strategy_id: str,
               symbol: str, order_type: OrderType, direction: int,
               limit_price: Optional[float] = None, stop_price: Optional[float] = None) -> Order:
        """Generates client_order_id, persists Order in PENDING, calls
        execution_adapter.submit_order, transitions state, publishes
        OrderSubmitted. Raises if sizing_decision.approved_quantity <= 0
        — never silently no-ops."""

    def handle_fill(self, fill: Fill) -> None:
        """Transitions order state (-> PARTIALLY_FILLED or FILLED),
        persists the Fill, publishes OrderFilled, updates the
        paper/live account balance and position."""

    def cancel(self, client_order_id: str) -> Order: ...
```

### `core/execution/latency_simulator.py`

```
class LatencySimulator:
    def __init__(self, base_ms: float, jitter_ms: float): ...
    def delay(self) -> float:
        """Returns a simulated latency in ms — fixed + uniform jitter.
        Deliberately NOT a queueing-theoretic model — see rationale in
        the architecture discussion: this project doesn't operate at a
        timescale where that fidelity pays for itself yet."""
```

### `core/execution/events.py`

```
@dataclass
class OrderSubmitted:
    client_order_id: str; strategy_id: str; symbol: str; mode: str; occurred_at: datetime

@dataclass
class OrderFilled:
    client_order_id: str; fill_price: float; quantity: float; is_partial: bool; occurred_at: datetime

@dataclass
class OrderRejected:
    client_order_id: str; reason: str; occurred_at: datetime

@dataclass
class OrderCancelled:
    client_order_id: str; occurred_at: datetime

@dataclass
class PaperFillSimulated:
    client_order_id: str; simulated_latency_ms: float; occurred_at: datetime
```

### `core/marketdata/websocket_connection.py`

```
class WebSocketConnection:
    """One per exchange (Stage 1: a fake/simulated feed for paper
    trading's market data needs; real exchange websockets are Stage 2).
    Exponential backoff reconnect, same retry taxonomy as ingestion's
    RetryPolicy. Heartbeat: if no message within timeout, force
    reconnect rather than silently going stale."""

    def __init__(self, url: str, on_message: Callable, heartbeat_timeout_s: float): ...
    def connect(self) -> None: ...
    def is_alive(self) -> bool: ...
```

### `core/marketdata/market_data_normalizer.py`

```
class MarketDataNormalizer:
    """Raw feed payload -> normalized internal event. Kept separate so
    Stage 2's real exchange payloads plug in here without touching
    anything downstream — same reasoning as pandas_ta_adapter.py being
    the only file that knows about the external library's shape."""

    def normalize(self, raw_payload: dict) -> "NormalizedTick": ...
```

## 4. Database schema changes

```sql
CREATE TABLE orders (
    client_order_id     TEXT PRIMARY KEY,
    exchange_order_id    TEXT,                -- null until Stage 2
    strategy_id           TEXT NOT NULL,
    symbol                 TEXT NOT NULL,
    order_type              TEXT NOT NULL,
    direction                 SMALLINT NOT NULL,
    quantity                   NUMERIC NOT NULL,
    limit_price                 NUMERIC,
    stop_price                   NUMERIC,
    mode                           TEXT NOT NULL,   -- 'paper' | 'live'
    state                           TEXT NOT NULL,
    risk_decision_id                 BIGINT NOT NULL REFERENCES risk_decision_log(id),
    created_at                         TIMESTAMPTZ NOT NULL,
    updated_at                           TIMESTAMPTZ NOT NULL
);

CREATE TABLE fills (
    id                  BIGSERIAL PRIMARY KEY,
    client_order_id      TEXT NOT NULL REFERENCES orders(client_order_id),
    fill_price            NUMERIC NOT NULL,
    quantity               NUMERIC NOT NULL,
    fee                      NUMERIC NOT NULL,
    is_partial                BOOLEAN NOT NULL,
    filled_at                   TIMESTAMPTZ NOT NULL
);

CREATE TABLE paper_accounts (
    account_id          TEXT PRIMARY KEY,
    starting_balance      NUMERIC NOT NULL,
    current_cash           NUMERIC NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL
);

CREATE TABLE account_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    account_id            TEXT NOT NULL REFERENCES paper_accounts(account_id),
    equity                  NUMERIC NOT NULL,
    open_position_count       INT NOT NULL,
    snapshot_at                 TIMESTAMPTZ NOT NULL
);
```

Note: no `balances`/reconciliation/external-trade tables in Stage 1 —
those are meaningless without a real exchange connection and belong to
Stage 2/3.

## 5. Testing strategy

- `test_order_state_machine.py`: every legal transition succeeds, every
  illegal transition (e.g. `FILLED -> SUBMITTED`) raises, tested
  exhaustively — this is the highest-value test file in the whole
  spec, since Stage 2/3 both depend on this state machine being correct.
- `test_paper_execution_adapter.py`: fills use the existing
  `ExecutionModel` correctly (fee/slippage numbers match what
  `BacktestEngine`'s tests already established), latency is applied
  and recorded.
- `test_order_manager_integration.py`: submit an order backed by a
  `SizingDecision` with `approved_quantity == 0` and confirm it's
  rejected, never silently accepted; full lifecycle test market order
  submit -> fill -> state `FILLED` -> account balance updated correctly.
- `test_websocket_connection.py`: against a fake local WebSocket
  server, not any real exchange — test reconnect-on-drop and
  heartbeat-triggered reconnect explicitly, including a test that
  asserts it does NOT reconnect in a tight loop (backoff working).
- Idempotency test: submit the same `client_order_id` twice, confirm
  no duplicate order is created.

## 6. Step-by-step build order

1. `Order`, `Fill`, `OrderState`, `OrderType` + `orders`/`fills` tables + state machine transition tests (no adapter yet).
2. `ExecutionAdapter` interface + `LiveExecutionAdapter` stub (raises `NotImplementedError` everywhere — exists only to prove the interface is adapter-agnostic).
3. `LatencySimulator` + `PaperExecutionAdapter` (wired to the existing `ExecutionModel`) + `paper_accounts`/`account_snapshots` tables + tests.
4. `OrderManager` wired to `PaperExecutionAdapter`, requiring a `SizingDecision` on every submit + integration tests.
5. `WebSocketConnection` + `MarketDataNormalizer` against a fake feed + reconnect/heartbeat tests.
6. Wire `PaperExecutionAdapter` to consume normalized market data from the WebSocket layer for realistic paper fills.
7. `CLAUDE.md` update, explicitly noting Stage 2 (real exchange adapters) and Stage 3 (live enablement, key custody) are NOT started and need their own specs.

## 7. Definition of done (Stage 1 only)

- A paper account can submit a market order through `OrderManager`,
  receive a simulated fill via `PaperExecutionAdapter`, and show a
  correct balance/position afterward.
- All tests in section 5 passing, shown, not assumed.
- `LiveExecutionAdapter` exists as an interface stub only — confirms
  the abstraction is real, without any Stage 2/3 work smuggled in.
