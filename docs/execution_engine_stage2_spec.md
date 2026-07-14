# Execution Engine — Stage 2 implementation specification (real Binance execution)

Status: approved architecture (Stage 2 of 3). Read alongside `CLAUDE.md`
and `docs/execution_engine_stage1_spec.md`. This stage implements real,
authenticated order placement against **Binance testnet only** —
mainnet credentials, API key encryption, and live-trading enablement
are Stage 3, not started here.

## 1. Locked-in decisions

| # | Decision |
|---|----------|
| 1 | Binance **testnet only** for this stage. Mainnet is explicitly out of scope until Stage 3's key custody work exists. |
| 2 | Credentials from environment variables only — no DB persistence, no multi-user credential management (both Stage 3). |
| 3 | Reconciliation is scoped to **our own tracked orders** (matched by `client_order_id`) — external/manual trade detection is deferred (see Open Decisions). |
| 4 | The REST reconciliation poll is authoritative over the WebSocket stream whenever they disagree — the stream is a low-latency notifier, never the sole source of truth. |
| 5 | Every order is pre-validated against cached Binance symbol filters (`LOT_SIZE`, `PRICE_FILTER`, `MIN_NOTIONAL`) before submission. |
| 6 | On any ambiguous submission failure (timeout, connection drop), the adapter queries the exchange for the existing `client_order_id` before ever retrying with a new one — never assumes failure. |
| 7 | System clock offset against Binance server time is tracked and applied to every signed request, not assumed accurate. |

## 2. Responsibilities

Owns: translating an `Order` (already approved by the Risk Engine,
already in `PENDING` state per Stage 1's state machine) into a real
Binance testnet order; tracking it via both WebSocket and REST poll;
reconciling any disagreement; classifying and surfacing exchange
errors. **Must never**: bypass `OrderManager`'s existing state machine,
accept mainnet credentials, or treat the WebSocket stream as
authoritative over a REST-confirmed state.

## 3. Architecture

Diagrammed above. `OrderManager` (Stage 1, unchanged) calls the new
`BinanceExecutionAdapter` exactly as it would call any
`ExecutionAdapter` implementation — this stage adds no new caller-side
logic to `OrderManager`. `BinanceExecutionAdapter` owns REST calls,
symbol filter validation, and error classification. A separate
`ListenKeyManager` + WebSocket consumer feeds low-latency order update
events. A separate `ReconciliationJob` polls REST directly on a
schedule and is the final arbiter of state.

## 4. Components

- **`BinanceExecutionAdapter(ExecutionAdapter)`** — replaces the
  Stage 1 `LiveExecutionAdapter` stub. Implements `submit_order`,
  `cancel_order`, `get_order_status` against Binance testnet's signed
  REST API.
- **`SymbolFilterCache`** — fetches and caches `GET /exchangeInfo`;
  validates quantity/price/notional against a symbol's filters before
  any order is sent.
- **`BinanceErrorClassifier`** — maps Binance error codes to the
  existing retry taxonomy: retryable (network, 5xx, 429/418
  rate-limit) vs. order-rejected-but-not-retryable (insufficient
  balance, filter violation) vs. fatal (bad signature, invalid
  symbol, account restricted).
- **`ClockSyncService`** — periodically calls `GET /api/v3/time`,
  tracks the offset, applied to every signed request's timestamp.
- **`ListenKeyManager`** — obtains and renews the user data stream's
  `listenKey` on the required cadence; a dead/expired key triggers a
  full reconnect with a fresh key, not a silent stall.
- **`BinanceOrderStreamConsumer`** — subscribes to the user data
  stream, normalizes execution reports, calls
  `OrderManager.handle_fill` — reusing Stage 1's existing method, no
  new fill-handling path.
- **`ReconciliationJob`** — scheduled (via the existing `Scheduler`),
  polls `GET /order` for every locally-open order, compares against
  the local record, corrects mismatches, logs every correction.

## 5. Data models

```python
@dataclass
class SymbolFilters:
    symbol: str
    min_qty: float; max_qty: float; step_size: float
    min_price: float; max_price: float; tick_size: float
    min_notional: float
    fetched_at: datetime

@dataclass
class ExchangeErrorClassification:
    retryable: bool
    category: str        # 'network' | 'rate_limit' | 'rejected' | 'fatal' | 'auth'
    binance_code: int
    message: str

@dataclass
class ReconciliationResult:
    client_order_id: str
    local_state: OrderState
    exchange_state: OrderState
    mismatch: bool
    corrected: bool
```

## 6. Database changes

```sql
CREATE TABLE symbol_filters_cache (
    symbol          TEXT PRIMARY KEY,
    min_qty          NUMERIC, max_qty NUMERIC, step_size NUMERIC,
    min_price          NUMERIC, max_price NUMERIC, tick_size NUMERIC,
    min_notional          NUMERIC,
    fetched_at              TIMESTAMPTZ NOT NULL
);

CREATE TABLE reconciliation_log (
    id                  BIGSERIAL PRIMARY KEY,
    client_order_id       TEXT NOT NULL REFERENCES orders(client_order_id),
    local_state             TEXT NOT NULL,
    exchange_state             TEXT NOT NULL,
    mismatch                     BOOLEAN NOT NULL,
    corrected                      BOOLEAN NOT NULL,
    checked_at                       TIMESTAMPTZ NOT NULL
);

-- Stage 1's orders table already has exchange_order_id (nullable) —
-- this stage is the first to actually populate it.
```

## 7. Event flow

New events (same `EventBus`, no new transport): `OrderAcknowledgedByExchange`,
`ExchangeOrderMismatchDetected`, `ExchangeOrderCorrected`,
`ListenKeyRenewed`, `ListenKeyExpiredReconnecting`,
`ExchangeErrorClassified`. `ReconciliationJob` is scheduler-triggered,
not event-triggered — it must run on a fixed cadence regardless of
whether anything seemed wrong, since the entire point is catching
problems nothing else noticed.

## 8. Testing strategy

- **Standard suite (no real network)**: `BinanceErrorClassifier`,
  `SymbolFilterCache` validation logic, `ReconciliationJob`'s
  mismatch-detection and correction logic — all tested against a fake
  adapter returning scripted responses, exactly like every other
  exchange-facing component in this project.
- **Idempotency test**: simulate a submission that times out
  ambiguously; assert the retry queries the existing `client_order_id`
  via `get_order_status` before ever calling `submit_order` again —
  and assert no duplicate order is created if the original had in fact
  succeeded.
- **Clock drift test**: inject an artificial offset, confirm signed
  requests carry the corrected timestamp.
- **ListenKey lifecycle test**: simulate approaching expiry, confirm
  renewal fires before expiry; simulate an already-expired key,
  confirm full reconnect with a fresh key rather than a retry loop
  against the dead one.
- **Reconciliation conflict test**: construct a local record saying
  `SUBMITTED` and a mocked exchange response saying `FILLED`; assert
  the local record is corrected to `FILLED`, a `Fill` is backfilled,
  and `ExchangeOrderMismatchDetected` + `ExchangeOrderCorrected` are
  both published — never a silent correction.
- **Testnet integration suite** (separate, marked, requires
  `BINANCE_TESTNET_API_KEY`/`SECRET` env vars, skipped if absent):
  place a real testnet market order, cancel a real testnet limit
  order, confirm a real WebSocket fill notification arrives and
  matches what reconciliation independently confirms via REST.

## 9. Integration points

- **`OrderManager` (Stage 1)**: unchanged — calls `BinanceExecutionAdapter`
  through the existing `ExecutionAdapter` interface. If `OrderManager`
  needed to change for this stage to work, that would indicate the
  Stage 1 interface wasn't actually adapter-agnostic; it should not
  need to change.
- **Risk Engine**: unchanged — every order still originates from an
  approved `SizingDecision`; Stage 2 doesn't touch that gate.
- **Rate limiter / retry policy (Historical Data Ingestion)**: reused
  directly, extended with Binance's trading-endpoint weight costs
  rather than building a second rate limiter.
- **Scheduler**: triggers `ReconciliationJob` and `ListenKeyManager`'s
  keepalive, same mechanism as every other scheduled job in this
  project.

## 10. Step-by-step build order

1. `ClockSyncService` + `SymbolFilterCache` — pure infrastructure, no order placement yet, tested against a fake Binance response.
2. `BinanceErrorClassifier` + tests covering the full retryable/rejected/fatal taxonomy.
3. `BinanceExecutionAdapter.submit_order`/`cancel_order`/`get_order_status` against testnet, with pre-submission filter validation and the ambiguous-failure idempotency handling — this is the highest-value step, test thoroughly before moving on.
4. `ListenKeyManager` + `BinanceOrderStreamConsumer`, wired to `OrderManager.handle_fill`, with the expiry/renewal tests.
5. `ReconciliationJob`, scheduled, with the conflict-correction tests.
6. Run the testnet integration suite end-to-end: submit, get a WebSocket fill notification, confirm reconciliation independently agrees.
7. Update `CLAUDE.md` — explicitly note mainnet credentials, key encryption, and live enablement remain entirely unimplemented (Stage 3), so this doesn't get mistaken for "trading is live."

## 11. Open decisions

1. **External/manual trade detection** (an order appearing on the
   exchange with no matching `client_order_id`) is explicitly deferred
   — confirmed: leave it for Stage 3 rather than folding it into
   Stage 2's reconciliation now.
2. **Reconciliation polling interval** — confirmed: 60s for open
   orders, configurable.
3. **Testnet data staleness** — confirmed: integration-suite results
   are a correctness check, not a performance/slippage benchmark.

## 12. Architect sign-off

Approved as designed above, with the testnet-only scoping in decision
#1 treated as non-negotiable — that's the one point in this spec I
would block on rather than defer if it were proposed differently.
