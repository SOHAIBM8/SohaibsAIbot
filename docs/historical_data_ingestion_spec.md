# Historical data ingestion — implementation specification

Status: approved architecture, ready for implementation.
Read alongside `CLAUDE.md` — the 10 project rules and working style apply
unchanged. This spec adds ingestion-specific requirements on top of them.

## 1. Purpose & scope

Ingest OHLCV candle data from crypto exchanges (Binance first) into
Postgres, reliably enough that every downstream backtest, feature, and
metric can trust the data without separately re-verifying it. This
component owns: backfill, incremental updates, gap detection and repair,
and ongoing data quality auditing. It does NOT own: feature computation
(already built), order execution, or real-time tick/order-book data
(explicitly out of scope — see Non-goals).

## 2. Locked-in architectural decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | TimescaleDB enabled on `raw_ohlcv` now, not deferred | Largest table in the system; time-series queries are fundamental; hypertables/compression/retention/continuous aggregates pay off immediately |
| 2 | Event bus = Postgres LISTEN/NOTIFY, behind an `EventBus` interface | No new infrastructure at current scale; the interface means Kafka/Redis Streams/NATS can replace the transport later without touching publishers or subscribers |
| 3 | Earliest-available-data discovery first, configurable fallback (default 5 years) if discovery fails | Avoids hardcoding a start date that's wrong for newer listings |
| 4 | Gap repair: max 3 attempts, ≥24h apart, then `confirmed_absent` | Bounded — a permanent hole in exchange history shouldn't retry forever; 24h spacing avoids marking a gap permanent during a transient multi-hour outage |
| 5 | Full observability: structured logs, health endpoint, metrics, monitoring hooks | Needed before this feeds a dashboard; also the only way to distinguish "ingestion is broken" from "the market actually did this" |
| 6 | Every service idempotent | Re-running any job must never duplicate or corrupt data — enforced at the DB constraint level, not just application logic |
| 7 | Every run fully reproducible and auditable | What was requested, received, stored, validated, retried, skipped — and why — must be reconstructable after the fact, never silent |
| 8 | Data Quality Service added as its own component | Distinct question from "did ingestion succeed" — this asks "is the data actually correct," including drift after storage and cross-checks against the exchange |

## 3. Data model

### 3.1 `raw_ohlcv` (TimescaleDB hypertable)

```
exchange           text        not null
symbol             text        not null
timeframe          text        not null
open_time          timestamptz not null
open, high, low, close   numeric not null
volume             numeric     not null
is_closed          boolean     not null default true   -- must ALWAYS be true; enforce via CHECK
ingested_at        timestamptz not null default now()
source_run_id      bigint      references ingestion_run_log(run_id)

primary key (exchange, symbol, timeframe, open_time)
```

Hypertable partitioned on `open_time`. Configure:
- **Chunk interval**: start at 7 days; revisit once real ingestion volume is observed (don't guess a smaller interval preemptively — this is exactly the kind of premature-optimization rule 8 warns against).
- **Compression policy**: compress chunks older than 30 days.
- **Retention policy**: none by default (we want full history) — leave the hook in place, don't enable a deletion policy without an explicit decision later.
- **Continuous aggregate**: one for daily OHLCV rolled up from 1h/1m data, refreshed on a schedule — this is what "hypertables pay off immediately" cashes out to concretely; low cost to add now, useful the moment multi-timeframe queries matter.

### 3.2 `ingestion_watermark`

```
exchange, symbol, timeframe     (primary key, composite)
earliest_available_at           timestamptz   -- discovered or fallback
last_ingested_open_time         timestamptz
backfill_complete               boolean
last_gap_scan_at                timestamptz
last_data_quality_check_at      timestamptz
updated_at                      timestamptz
```

### 3.3 `ingestion_gap`

```
gap_id             bigserial primary key
exchange, symbol, timeframe
gap_start, gap_end timestamptz
status             text   -- 'pending' | 'repaired' | 'confirmed_absent'
attempts            int   default 0
last_attempt_at     timestamptz
next_attempt_after  timestamptz   -- enforces the 24h spacing rule
detected_at         timestamptz
resolved_at         timestamptz
```

### 3.4 `ingestion_run_log`

```
run_id              bigserial primary key
run_type            text   -- 'backfill' | 'incremental' | 'gap_repair' | 'data_quality'
exchange, symbol, timeframe
started_at, finished_at   timestamptz
status              text   -- 'success' | 'partial' | 'failed'
requested_range      jsonb   -- what was asked for
received_count       int    -- what came back from the exchange
stored_count         int    -- what actually got persisted (may differ due to validation rejects)
validation_failures   jsonb  -- list of {open_time, reason}
retries              int
skipped_reason        text   -- null unless the run skipped work, and why
error_message         text
```

This table is the answer to requirement 7 (full reproducibility/audit)
by itself — every other requirement in that list is a column here.

### 3.5 `tracked_instruments`

```
exchange, symbol, timeframe   (primary key, composite)
active               boolean
added_at             timestamptz
```

Adding coverage is a row insert here, never a code change.

### 3.6 `data_quality_report`

```
report_id           bigserial primary key
exchange, symbol, timeframe
run_at               timestamptz
checks_run           jsonb   -- which checks executed
issues_found         jsonb   -- structured list: {check, severity, detail}
candles_checked      int
cross_check_diffs    int     -- count of stored vs re-fetched mismatches
summary              text    -- human-readable one-paragraph summary
```

## 4. Component specifications

Interfaces below are signatures and responsibilities — implementation
logic is for Claude Code to write, following these contracts and the
project rules (typed, tested, documented, no premature optimization).

### 4.1 `ExchangeAdapter` (interface, one implementation per exchange)

```
class ExchangeAdapter(ABC):
    exchange_name: str
    rate_limit_config: RateLimitConfig

    @abstractmethod
    def fetch_klines(self, symbol, timeframe, start_time, end_time, limit) -> list[RawCandle]: ...

    @abstractmethod
    def earliest_available(self, symbol, timeframe) -> datetime | None:
        """Discover the earliest candle the exchange can return, e.g. via
        binary search or a listing-date endpoint. Return None if
        undiscoverable — caller falls back to the configured default."""

    @abstractmethod
    def normalize_symbol(self, symbol: str) -> str: ...
```

`BinanceAdapter` implements this first. `BybitAdapter`/`MEXCAdapter` are
later files implementing the same interface — nothing else in the
codebase should need to change when they're added.

### 4.2 `RateLimiter`

Per-exchange, configured from `RateLimitConfig` (weight-per-request,
requests-per-window — these differ by exchange, never hardcode a global
constant). Wraps every adapter call; blocks/queues rather than exceeding
limits; surfaces 429/418 responses to the retry policy rather than
swallowing them.

### 4.3 `RetryPolicy`

Exponential backoff with jitter. Explicit error taxonomy:
- **Retryable**: timeout, 5xx, 429 (rate limit)
- **Fatal, no retry**: 400 (bad request), invalid symbol, malformed response shape

### 4.4 `CandleValidator`

Pure functions, no I/O:
- `high >= max(open, close)`, `low <= min(open, close)`, `high >= low`
- `volume >= 0`
- `open_time` aligned to the timeframe boundary (e.g. 1h candles land on the hour)
- `is_closed` — reject and log (never store) any candle where `close_time >= now()`
- No duplicate `open_time` within the same batch

Every rejection is recorded in `ingestion_run_log.validation_failures`
with a reason — never silently dropped.

### 4.5 `BackfillService`

For a `(exchange, symbol, timeframe)`:
1. Call `adapter.earliest_available()`; fall back to
   `now() - config.default_backfill_years` (default 5) if discovery
   fails or returns `None`.
2. Paginate forward from that point to now, respecting the exchange's
   per-request candle limit.
3. Validate each batch; upsert via `ON CONFLICT DO NOTHING` (closed
   candles are immutable — don't overwrite on conflict by default).
4. Update `ingestion_watermark` and write one `ingestion_run_log` row
   per invocation (not per batch — one row for the whole backfill run,
   with `received_count`/`stored_count` aggregated).
5. Idempotent by construction: re-running finds already-ingested ranges
   via the unique constraint and no-ops them.

### 4.6 `IncrementalUpdateService`

Scheduled per `(exchange, symbol, timeframe)` at a cadence appropriate
to that timeframe (1m polled far more often than 1d — cadence is
config, not hardcoded per-service logic). Fetches only candles after
`ingestion_watermark.last_ingested_open_time`, validates, upserts,
advances the watermark, publishes a `CandlesIngested` event.

### 4.7 `GapDetectionService`

Compares expected timestamps (`generate_series` at the timeframe's
interval, from `earliest_available_at` to `last_ingested_open_time`)
against actual rows in `raw_ohlcv`. Any missing timestamp becomes (or
updates) a row in `ingestion_gap` with status `pending`.

### 4.8 `GapRepairService`

For each `pending` gap where `next_attempt_after <= now()`:
1. Re-fetch that specific range via the adapter.
2. If data comes back: validate, upsert, mark `repaired`, set `resolved_at`.
3. If still missing: increment `attempts`, set
   `next_attempt_after = now() + 24h`.
4. After the 3rd unsuccessful attempt: mark `confirmed_absent` — this
   is a terminal state; the gap scanner should not re-flag it in
   future scans (this is a genuine "no data exists here," not an
   ingestion failure).

### 4.9 `EventBus` (interface + Postgres LISTEN/NOTIFY implementation)

```
class EventBus(ABC):
    @abstractmethod
    def publish(self, event: IngestionEvent) -> None: ...
    @abstractmethod
    def subscribe(self, event_type: str, handler: Callable) -> None: ...

class PostgresEventBus(EventBus):
    """LISTEN/NOTIFY implementation. Swappable later for Kafka/Redis
    Streams/NATS without changing any publisher or subscriber code —
    that guarantee is the entire point of the interface existing."""
```

Events: `CandlesIngested`, `GapDetected`, `GapRepaired`,
`BackfillCompleted`, `DataQualityIssueFound`.

### 4.10 `Scheduler`

Single long-running containerized process (in-process scheduling
library — no Airflow/Prefect at this stage; that's the upgrade path if
job orchestration complexity actually grows across many
exchanges/symbols, not a day-one requirement). Coordinates: backfill
trigger on new `tracked_instruments` rows, incremental update jobs per
timeframe cadence, nightly gap detection, nightly data quality checks,
gap repair sweeps respecting `next_attempt_after`.

### 4.11 `DataQualityService` (new)

Runs nightly per `(exchange, symbol, timeframe)`. Distinct from gap
detection: this checks *correctness*, not just *completeness*.

Checks:
- Missing candles (cross-reference with `GapDetectionService`'s findings)
- Duplicate candles (should be structurally impossible given the unique
  constraint — checking anyway catches a constraint that was somehow
  bypassed, e.g. a manual DB write)
- Invalid OHLC values (reruns `CandleValidator` against already-stored
  data — catches corruption introduced after storage, not just at
  ingestion time)
- Timestamp alignment drift
- Volume anomalies (statistical outlier check against trailing
  distribution — flag, don't auto-reject; a real volume spike is valid
  data, this is for human review)
- Timeframe consistency (e.g. summing 1m candles for an hour should
  reconcile with the stored 1h candle for that hour, within tolerance)
- **Cross-check against exchange**: re-fetch a recent sample directly
  from the adapter and diff against what's stored — this is the one
  check that catches data drifting wrong *after* it was correctly
  ingested (disk corruption, a bad manual edit, a bug in an unrelated
  process touching the table)

Output: one `data_quality_report` row per run, plus a
`DataQualityIssueFound` event for anything above a severity threshold,
so a monitoring hook can alert on it rather than requiring someone to
read the table.

### 4.12 Observability

- **Structured logging**: every service logs via the existing
  `structlog` setup (`core/logging_config.py`) — key=value context, not
  interpolated strings, consistent with the rest of the codebase.
- **Health endpoint**: `/health` — checks DB connectivity and recency of
  the last successful run per tracked instrument; used for
  container/orchestration liveness checks.
- **Metrics endpoint**: `/metrics` in Prometheus text format (via
  `prometheus_client` or equivalent — Claude Code's call at
  implementation time). Minimum metric set: candles ingested (counter),
  API latency (histogram), retry count (counter), repair success rate
  (gauge/counter pair), gap count (gauge), duplicate count (counter,
  should stay at zero), validation failure count (counter).
- These feed the future dashboard — the metric names should be treated
  as a stable interface once implemented, not renamed casually later.

## 5. Configuration

Application config (env vars or a config file, following the existing
`config/regime_detector.yaml` pattern):
- `default_backfill_years` (default 5)
- `gap_repair_max_attempts` (default 3)
- `gap_repair_retry_interval_hours` (default 24)
- Per-exchange rate limit settings
- Per-timeframe incremental polling cadence
- TimescaleDB chunk interval, compression age threshold

## 6. Idempotency & reproducibility — explicit guarantees

- Re-running backfill, incremental update, or gap repair for the same
  range never duplicates rows (DB unique constraint) and never
  corrupts existing closed candles (no update-on-conflict for closed data).
- Every run, regardless of type, produces exactly one `ingestion_run_log`
  row answering: what was requested, what came back, what was stored,
  what failed validation and why, how many retries occurred, and if
  anything was skipped, why.
- Nothing is fire-and-forget — a job that does nothing (e.g., "no new
  candles since last watermark") still logs that outcome, not silence.

## 7. Testing requirements

- `ExchangeAdapter` gets a fake/mock implementation returning
  deterministic, scripted candle sequences — including deliberately
  malformed ones — so every other component's tests run without any
  real network call.
- `CandleValidator`: unit tests per rule (bad OHLC ordering, misaligned
  timestamp, forming candle, negative volume).
- `BackfillService`/`IncrementalUpdateService`: test against the fake
  adapter for correct pagination, correct watermark advancement, and
  idempotency (run twice, assert no duplicate rows and identical final state).
- `GapDetectionService`/`GapRepairService`: test gap discovery against a
  deliberately incomplete fake dataset; test the 3-attempt/24h-spacing
  state machine explicitly, including the `confirmed_absent` terminal state.
- `EventBus`: test that `PostgresEventBus` delivers published events to
  subscribers; test against the interface so a future transport swap
  can reuse the same test suite.
- `DataQualityService`: test each check independently against
  constructed bad data (inject a duplicate, inject a misaligned
  timestamp, inject an OHLC violation) and confirm each is caught and
  reported.
- All DB-touching tests run against the real local Postgres (via
  `docker compose up -d`), not mocks — consistent with how the
  Experiment Tracker was tested.

## 8. Non-goals for this phase (explicitly out of scope)

- Real-time websocket/tick-level ingestion — this component is
  candle-level REST polling only.
- Order book data.
- Multi-region storage/replication.
- A full job orchestrator (Airflow/Prefect/Dagster) — revisit only if
  the in-process scheduler genuinely can't keep up.

## 9. Definition of done

- Binance backfill, incremental update, gap detection/repair, and data
  quality checks all working end-to-end against real Binance data for
  at least one symbol/timeframe.
- All tests in section 7 passing, run and shown, not assumed.
- Health and metrics endpoints reachable and returning real data.
- `CLAUDE.md` updated with what was built, matching the pattern from
  every prior component.
