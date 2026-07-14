# AI Analysis & Signal Explanation Engine — implementation specification

Status: approved architecture, ready for implementation. Read alongside
`CLAUDE.md`, `docs/risk_engine_spec.md`, and
`docs/execution_engine_stage1_spec.md`. The 10 project rules apply
unchanged. This component is strictly downstream and read-only with
respect to every existing component — see section 1 for the concrete
guarantees, not just the principle.

## 1. Locked-in decisions

| # | Decision |
|---|----------|
| 1 | `llm_readonly` Postgres role: `SELECT`-only on all trading tables, no write grant of any kind. `ContextBuilder` and every `ChatTool` connect using this role, not the default app role. |
| 2 | `ChatToolRegistry` injects `account_id` from the authenticated session into every tool call itself; any account/user identifier the LLM attempts to supply is discarded, never honored. |
| 3 | `LLMUsageTracker` checks and increments a daily counter *before* every call and hard-stops once the configured cap is reached — the cap is enforced code, not a config value nothing reads. |
| 4 | `NewsSourceAdapter`/`NewsSourceRegistry` and `ChatTool`/`ChatToolRegistry` are structural copies of the existing `StrategyBase`/`StrategyRegistry` pattern — same discovery-and-validate-at-registration approach, not a new pattern. |
| 5 | Prompt templates are versioned and persisted (`prompt_templates` table), referenced by id from every generated explanation — reproducibility of *what was asked*, matching `ExperimentConfig.code_commit_hash`'s reasoning. |
| 6 | Zero write path from this component back into any trading table, enforced at the database grant level, not just by omission in application code. |
| 7 | Explanation generation is on-demand or nightly-scheduled only — never triggered synchronously from a trading event. |

## 2. Folder structure

```
core/ai_assistant/
    __init__.py
    news_source_adapter.py        # NewsSourceAdapter interface (mirrors ExchangeAdapter)
    news_source_registry.py        # NewsSourceRegistry (mirrors StrategyRegistry)
    news_ingestion_service.py       # orchestrates adapters -> news_articles
    context_builder.py               # ContextBuilder + context dataclasses
    prompt_template.py                # PromptTemplate + PromptTemplateRegistry
    llm_client.py                      # wraps Claude API calls
    llm_usage_tracker.py                # enforces daily cost/call cap
    explanation_cache.py                 # get_or_generate(), keyed to grounding-fact hash
    chat_tool.py                          # ChatTool interface + concrete read-only tools
    chat_tool_registry.py                  # ChatToolRegistry, enforces account_id scoping
    chat_query_service.py                   # orchestrates LLMClient + tools + llm_query_log
    daily_summary_job.py                     # Scheduler-triggered nightly digest
    events.py                                  # event dataclasses

config/
    ai_assistant.yaml              # model name, daily cost cap, active prompt template ids

tests/test_ai_assistant/
    __init__.py
    test_news_source_registry.py
    test_context_builder.py
    test_prompt_template_registry.py
    test_llm_client.py                      # fake LLM only, no real API calls
    test_llm_usage_tracker.py
    test_explanation_cache.py
    test_chat_tool_registry.py              # security: no mutating tools, scoping enforced
    test_chat_query_service_integration.py
    test_daily_summary_job.py
    test_readonly_role_enforcement.py       # DB-level: attempt a write, confirm Postgres rejects it
    test_prompt_injection_resistance.py     # crafted input, confirm no cross-account access

# MODIFIED existing files
schema.sql             # new tables, llm_readonly role + grants
CLAUDE.md               # updated after implementation
```

## 3. Every class, interface, dataclass, enum

### `core/ai_assistant/news_source_adapter.py` / `news_source_registry.py`

```
class NewsSourceAdapter(ABC):
    source_name: str
    @abstractmethod
    def fetch_recent(self, since: datetime) -> list[RawArticle]: ...

class NewsSourceRegistry:
    """Structural copy of StrategyRegistry: discover(), validate at
    registration, get_all()."""
    def discover(self, package="news_sources"): ...
```

### `core/ai_assistant/context_builder.py`

```
@dataclass
class TradeExplanationContext:
    order: Order; fills: list[Fill]; signal: SignalLogEntry
    risk_decision: RiskDecisionLogRow; regime_at_entry: str

@dataclass
class RiskDecisionContext:
    decision: RiskDecisionLogRow; layer_results: list[LayerResult]

@dataclass
class RegimeContext:
    symbol: str; window_start: datetime; window_end: datetime
    regime_history: list[dict]     # pulled from signal_log's regime columns

@dataclass
class DailySummaryContext:
    account_id: str; date: date
    trades: list[Trade]; risk_decisions: list[RiskDecisionLogRow]
    equity_start: float; equity_end: float

class ContextBuilder:
    """Connects via the llm_readonly role. Every method pulls EXACTLY
    the relevant rows for the given id/account — no broader query, no
    inference, no join beyond what's needed to answer this one
    subject."""
    def __init__(self, readonly_db_session): ...
    def build_trade_context(self, order_id: str) -> TradeExplanationContext: ...
    def build_risk_decision_context(self, decision_id: int) -> RiskDecisionContext: ...
    def build_regime_context(self, symbol: str, start: datetime, end: datetime) -> RegimeContext: ...
    def build_daily_summary_context(self, account_id: str, date: date) -> DailySummaryContext: ...
```

### `core/ai_assistant/prompt_template.py`

```
@dataclass
class PromptTemplate:
    template_id: str; version: str; subject_type: str
    template_text: str; created_at: datetime

class PromptTemplateRegistry:
    """Versioned like StrategyMeta. Templates are the ONLY place system-
    prompt wording lives — never inlined ad hoc in LLMClient calls, so
    every generated explanation is traceable to an exact template
    version."""
    def get(self, template_id: str) -> PromptTemplate: ...
```

### `core/ai_assistant/llm_client.py`

```
@dataclass
class LLMResponse:
    text: str; tokens_used: int; tool_calls_made: list[str]
    model: str; latency_ms: float

class LLMClient:
    """Wraps the Claude API. Own credentials, entirely separate from
    any exchange API key storage. Checks LLMUsageTracker before every
    call."""
    def __init__(self, api_key_env_var: str, model: str, usage_tracker: "LLMUsageTracker"): ...
    def generate(self, system_prompt: str, user_content: str,
                 tools: Optional[list["ChatTool"]] = None) -> LLMResponse: ...
```

### `core/ai_assistant/llm_usage_tracker.py`

```
class LLMUsageTracker:
    def __init__(self, daily_cap_calls: int, db_session): ...
    def check_and_increment(self) -> bool:
        """Returns False (and does NOT increment) if the cap is already
        reached — caller must refuse the request, not proceed anyway."""
    def record_usage(self, tokens_used: int, cost_estimate: float) -> None: ...
```

### `core/ai_assistant/explanation_cache.py`

```
class ExplanationCache:
    def get_or_generate(
        self, subject_type: str, subject_id: str,
        context_fn: Callable[[], object], template: PromptTemplate,
        llm_client: LLMClient,
    ) -> "Explanation":
        """Hashes the context_fn() output; on hash match, returns the
        cached row without calling the LLM. On miss, generates, persists,
        publishes ExplanationGenerated."""

@dataclass
class Explanation:
    explanation_id: int; subject_type: str; subject_id: str
    generated_text: str; prompt_version: str; generated_at: datetime
```

### `core/ai_assistant/chat_tool.py` / `chat_tool_registry.py`

```
class ChatTool(ABC):
    name: str
    description: str
    @abstractmethod
    def execute(self, account_id: str, **params) -> dict:
        """account_id is ALWAYS the value injected by ChatToolRegistry
        from the authenticated session — never read from params, even
        if the caller (including the LLM) supplies one."""

class GetTradeTool(ChatTool): ...
class GetRiskDecisionsTool(ChatTool): ...
class GetRegimeHistoryTool(ChatTool): ...
class SearchNewsTool(ChatTool): ...
# Deliberately no write-capable tool exists in this file or anywhere
# in this component — not "unused," structurally absent.

class ChatToolRegistry:
    """Structural copy of StrategyRegistry's discovery pattern."""
    def __init__(self, tools: list[ChatTool]): ...
    def execute_tool_call(self, account_id: str, tool_name: str, llm_supplied_params: dict) -> dict:
        """Strips any 'account_id'/'user_id' key from llm_supplied_params
        BEFORE dispatch, then calls tool.execute(account_id=<real session
        value>, **remaining_params)."""
```

### `core/ai_assistant/chat_query_service.py`

```
class ChatQueryService:
    def __init__(self, llm_client: LLMClient, tool_registry: ChatToolRegistry, db_session): ...
    def answer(self, account_id: str, question: str) -> str:
        """Logs the question, resolved tool calls, and response to
        llm_query_log unconditionally — every question asked and every
        tool invoked in answering it is auditable."""
```

### `core/ai_assistant/daily_summary_job.py`

```
class DailySummaryJob:
    """Triggered by the existing Scheduler, not a new scheduling
    mechanism."""
    def run_for_account(self, account_id: str, date: date) -> Explanation: ...
```

### `core/ai_assistant/events.py`

```
@dataclass
class ExplanationGenerated:
    subject_type: str; subject_id: str; occurred_at: datetime

@dataclass
class NewsIngested:
    source: str; article_count: int; occurred_at: datetime

@dataclass
class ChatQueryAnswered:
    account_id: str; query_id: int; occurred_at: datetime

@dataclass
class LLMUsageCapReached:
    date: date; occurred_at: datetime
```

## 4. Database schema changes

```sql
-- Dedicated read-only role. No INSERT/UPDATE/DELETE grant exists for
-- this role on ANY table, now or in the future — any table added
-- later (including a future exchange-key vault) must be deliberately
-- excluded from this role's grants, not deliberately included.
CREATE ROLE llm_readonly LOGIN PASSWORD :'llm_readonly_password';
GRANT CONNECT ON DATABASE trading_platform TO llm_readonly;
GRANT USAGE ON SCHEMA public TO llm_readonly;
GRANT SELECT ON
    signal_log, risk_decision_log, orders, fills,
    experiments, paper_accounts, account_snapshots,
    news_articles
TO llm_readonly;

CREATE TABLE news_articles (
    article_id     BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,
    url              TEXT NOT NULL,
    title             TEXT NOT NULL,
    published_at       TIMESTAMPTZ,
    ingested_at          TIMESTAMPTZ NOT NULL,
    raw_content            TEXT
);

CREATE TABLE prompt_templates (
    template_id    TEXT PRIMARY KEY,
    version         TEXT NOT NULL,
    subject_type     TEXT NOT NULL,   -- 'trade' | 'risk_decision' | 'regime' | 'daily_summary' | 'chat'
    template_text      TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL
);

CREATE TABLE llm_explanations (
    explanation_id    BIGSERIAL PRIMARY KEY,
    subject_type        TEXT NOT NULL,
    subject_id            TEXT NOT NULL,
    grounding_fact_hash     TEXT NOT NULL,
    prompt_template_id        TEXT NOT NULL REFERENCES prompt_templates(template_id),
    model_used                  TEXT NOT NULL,
    generated_text                TEXT NOT NULL,
    generated_at                    TIMESTAMPTZ NOT NULL,
    tokens_used                      INT,
    cost_estimate                      NUMERIC
);

CREATE TABLE llm_query_log (
    query_id        BIGSERIAL PRIMARY KEY,
    account_id        TEXT NOT NULL,
    question            TEXT NOT NULL,
    tool_calls_made       JSONB,
    response                TEXT,
    flagged_for_review        BOOLEAN NOT NULL DEFAULT FALSE,
    occurred_at                  TIMESTAMPTZ NOT NULL
);

CREATE TABLE llm_usage_daily (
    usage_date         DATE PRIMARY KEY,
    calls_made           INT NOT NULL DEFAULT 0,
    tokens_used             BIGINT NOT NULL DEFAULT 0,
    estimated_cost             NUMERIC NOT NULL DEFAULT 0,
    daily_cap_calls               INT NOT NULL,
    daily_cap_reached                BOOLEAN NOT NULL DEFAULT FALSE
);
```

## 5. Testing strategy

- `test_readonly_role_enforcement.py`: connect AS `llm_readonly` and
  attempt an `INSERT`/`UPDATE`/`DELETE` against a trading table;
  assert Postgres itself raises a permissions error. This is the one
  test in the whole spec that proves the guarantee at the right layer
  — a passing application-level test alone would not be sufficient.
- `test_prompt_injection_resistance.py`: feed a fake LLM a scripted
  response that attempts to call a tool with an `account_id` different
  from the session's, and separately attempts to invoke a nonexistent
  write-style tool name; assert the registry silently discards the
  injected account id and raises/rejects the unknown tool name — never
  guesses or falls through to a default that could leak data.
- `test_chat_tool_registry.py`: a structural test enumerating every
  registered tool and asserting none of their names/descriptions imply
  a mutation (`place`, `cancel`, `update`, `delete`, `set` — flag any
  tool name matching these patterns as a hard test failure, not just a
  lint warning).
- `test_llm_client.py`: fake LLM responses only, exactly like every
  other "no real network in unit tests" component in this project.
  Separately (not part of the standard suite), a small number of real-
  API integration tests, rate-limited, checked less frequently,
  confirming generated text for a fixed golden context doesn't
  reference facts outside it.
- `test_llm_usage_tracker.py`: cap reached mid-day — subsequent calls
  refused without incrementing further; cap resets at the next
  `usage_date`.
- `test_explanation_cache.py`: identical context hash — no second LLM
  call; changed context — cache miss, regenerates.
- `test_context_builder.py`: once more than one account exists (even
  if only in test fixtures for now), assert a context built for
  account A never includes account B's rows — write this test now,
  before multi-tenancy is real, so it's already true when it matters.

## 6. Step-by-step build order

Deliberately sequenced lowest-risk to highest-risk — explaining
already-logged trading data first (no new external input), news
second (new external data source), open chat last (the largest
security surface, built once every pattern below it is proven).

1. `PromptTemplate` + `PromptTemplateRegistry` + `prompt_templates` table. No LLM calls yet — just versioned template storage.
2. `llm_readonly` role + grants + `ContextBuilder` (trade/risk-decision/regime contexts only) + `test_readonly_role_enforcement.py`.
3. `LLMUsageTracker` + `llm_usage_daily` table + cap-enforcement tests.
4. `LLMClient` (fake-LLM-tested) + `ExplanationCache` + `llm_explanations` table — produces trade/risk-decision/regime explanations end-to-end. Zero news, zero chat, zero write surface beyond this component's own tables.
5. `DailySummaryJob` wired to the existing `Scheduler` + `DailySummaryContext`.
6. `NewsSourceAdapter` + `NewsSourceRegistry` + one concrete adapter + `news_articles` table.
7. `ChatTool` (concrete read-only tools) + `ChatToolRegistry` with enforced account scoping + `llm_query_log` table + the security test suite (`test_chat_tool_registry.py`, `test_prompt_injection_resistance.py`) — do not skip or defer these two files.
8. `ChatQueryService` wiring `LLMClient` + `ChatToolRegistry` + `llm_query_log`, integration-tested end to end.
9. Update `CLAUDE.md`.

## 7. Definition of done

- A real trade's explanation can be generated end-to-end (steps 1-4) with the actual Claude API, grounded correctly, cached correctly.
- `test_readonly_role_enforcement.py` and `test_prompt_injection_resistance.py` both passing — these two are the acceptance bar for "no write path," not optional nice-to-haves.
- Daily cap demonstrably refuses a call once reached, in a real test, not just reviewed code.
- `CLAUDE.md` updated, explicitly stating this component has zero write access to any trading table, enforced at the database role level.
