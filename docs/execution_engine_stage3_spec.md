# Execution Engine — Stage 3 implementation specification (live trading security)

Status: approved architecture (Stage 3 of 3, the final execution
stage). Read alongside `CLAUDE.md`, `docs/execution_engine_stage1_spec.md`,
and `docs/execution_engine_stage2_spec.md`. This is the highest-stakes
spec in the project — a defect here compromises real credentials, not
a backtest number. Treat every "why" in this document as load-bearing.

## 1. Locked-in decisions

| # | Decision |
|---|----------|
| 1 | Envelope encryption: per-credential DEK, wrapped by a KEK held in an external KMS, never co-located with the encrypted data in our own database. |
| 2 | Withdrawal-permission validation runs at connection time AND on a recurring schedule — a stale one-time check is not acceptable. |
| 3 | Arming is scoped per (account, strategy, exchange), expires, and requires re-confirmation after any config change — distinct from, and checked independently alongside, the Risk Engine's existing `KillSwitch`. |
| 4 | `credential_audit_log` is `INSERT`-only at the database role level — no `UPDATE`/`DELETE` grant to any role, including the default app role. |
| 5 | `EmergencyCredentialRevocation` is a distinct, more severe action than the kill switch: invalidates cached decrypted material and requires explicit KMS re-grant before any credential can be decrypted again. |
| 6 | Mainnet credentials structurally require the real KMS path — the lightweight local/dev key-derivation path (fine for testnet) is code-level incapable of being used when `mainnet=True`, not merely discouraged by convention. |
| 7 | `BinanceExecutionAdapter`'s order-placement/cancellation/status logic (Stage 2) does not change — only how it obtains credentials changes, via a new `CredentialProvider` seam. |
| 8 | No plaintext credential value may ever appear in a log line, under any log level, anywhere in the codebase. |

## 2. Responsibilities

Owns: encrypting, storing, and controlling access to exchange API
credentials; validating and continuously re-validating their
permission scope; gating whether live (mainnet or testnet-beyond-
Stage-2) trading is currently permitted; auditing every credential
access; providing emergency revocation. **Must never**: alter
deterministic order logic, decrypt a credential without logging it,
or permit a mainnet-flagged credential through the dev KMS path.

## 3. Architecture

Diagrammed above. `CredentialVault` stores only encrypted material;
the KEK lives in an external KMS the application never persists
locally. `CredentialProvider` is the only thing `BinanceExecutionAdapter`
calls to obtain live credentials — it decrypts on demand and logs
every access before returning anything. Before any live order reaches
`BinanceExecutionAdapter`, two independent gates must both pass: the
existing `KillSwitch` and this stage's new `ArmingService`.

## 4. Components

- **`KMSClient`** (interface, pluggable like every other adapter
  interface in this project) — `AWSKMSClient`/`VaultKMSClient` for
  real KEK operations; `LocalDevKMSClient` for testnet-only local
  development, structurally rejected wherever `mainnet=True`.
- **`CredentialVault`** — envelope encryption: generates a per-credential
  DEK, encrypts the API key/secret with it, wraps the DEK with the
  KEK via `KMSClient`, stores only ciphertext.
- **`CredentialProvider`** — the seam `BinanceExecutionAdapter` depends
  on. `get_credentials(account_id, exchange)` decrypts on demand,
  writes a `credential_audit_log` row before returning, never caches
  plaintext beyond the immediate call.
- **`PermissionValidator`** — calls Binance's permission-check endpoint
  at connection time and on a scheduled recurring job; any
  withdrawal-enabled finding immediately transitions the credential to
  `VALIDATION_FAILED` and disarms trading for it.
- **`ArmingService`** — per (account, strategy, exchange) consent
  state: armed/disarmed, expiry, re-confirmation requirement after any
  parameter change.
- **`EmergencyCredentialRevocation`** — the panic-button action,
  distinct from `KillSwitch`.
- **`KeyLifecycleManager`** — credential state machine (see below) and
  rotation-due reminders.
- **`MainnetGate`** — structural guard: raises, does not warn, if any
  code path attempts to pair `mainnet=True` with `LocalDevKMSClient`.

## 5. Data models

```python
class CredentialState(Enum):
    PENDING_VALIDATION; ACTIVE; VALIDATION_FAILED; ROTATION_DUE; REVOKED

@dataclass
class EncryptedCredential:
    credential_id: str; account_id: str; exchange: str
    encrypted_api_key: bytes; encrypted_api_secret: bytes
    wrapped_dek: bytes; kek_key_id: str
    state: CredentialState; mainnet: bool
    created_at: datetime; last_validated_at: Optional[datetime]
    last_rotated_at: Optional[datetime]; rotation_due_at: Optional[datetime]

@dataclass
class ArmingState:
    account_id: str; strategy_id: str; exchange: str
    armed: bool; armed_at: Optional[datetime]; expires_at: Optional[datetime]
    armed_by: str; mainnet: bool

@dataclass
class CredentialAuditEntry:
    entry_id: int; credential_id: str
    action: str        # 'decrypted' | 'validated' | 'validation_failed' | 'revoked' | 'rotated'
    requested_by: str; client_order_id: Optional[str]
    occurred_at: datetime
```

## 6. Database changes

```sql
CREATE TABLE encrypted_credentials (
    credential_id      TEXT PRIMARY KEY,
    account_id           TEXT NOT NULL,
    exchange               TEXT NOT NULL,
    encrypted_api_key        BYTEA NOT NULL,
    encrypted_api_secret       BYTEA NOT NULL,
    wrapped_dek                  BYTEA NOT NULL,
    kek_key_id                     TEXT NOT NULL,
    state                            TEXT NOT NULL,
    mainnet                            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at                           TIMESTAMPTZ NOT NULL,
    last_validated_at                      TIMESTAMPTZ,
    last_rotated_at                          TIMESTAMPTZ,
    rotation_due_at                            TIMESTAMPTZ
);

CREATE TABLE arming_state (
    account_id      TEXT NOT NULL,
    strategy_id       TEXT NOT NULL,
    exchange            TEXT NOT NULL,
    armed                 BOOLEAN NOT NULL DEFAULT FALSE,
    armed_at                TIMESTAMPTZ,
    expires_at                 TIMESTAMPTZ,
    armed_by                     TEXT,
    mainnet                        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (account_id, strategy_id, exchange)
);

-- INSERT-only. No UPDATE/DELETE grant exists on this table, to ANY
-- role, ever — this is what makes the audit trail actually trustworthy.
CREATE TABLE credential_audit_log (
    entry_id          BIGSERIAL PRIMARY KEY,
    credential_id       TEXT NOT NULL REFERENCES encrypted_credentials(credential_id),
    action                 TEXT NOT NULL,
    requested_by              TEXT NOT NULL,
    client_order_id             TEXT,
    occurred_at                    TIMESTAMPTZ NOT NULL
);

CREATE ROLE credential_audit_writer;
GRANT INSERT ON credential_audit_log TO credential_audit_writer;
-- Deliberately no UPDATE/DELETE grant on credential_audit_log to any
-- role, including the default app role.
```

## 7. Event flow

New events: `CredentialDecrypted`, `CredentialValidationFailed`
(triggers auto-disarm + high-severity alert), `ArmingStateChanged`,
`ArmingExpired`, `EmergencyRevocationTriggered`, `KeyRotationDue`. All
via the existing `EventBus`. `CredentialValidationFailed` is treated
with the same severity as a `KillSwitchEngaged` event downstream —
alerting must not distinguish "risk breach" from "compromised-looking
credential" as more or less urgent.

## 8. Testing strategy

- **Encryption round-trip**: encrypt then decrypt returns the original
  value, tested against a `LocalDevKMSClient` fake — never a real
  cloud KMS in the unit suite.
- **`MainnetGate` structural test**: attempt to construct/use a
  `mainnet=True` credential with `LocalDevKMSClient`; assert it raises,
  not warns — this is the single most important test in this spec.
- **`PermissionValidator`**: scripted exchange response with withdrawal
  enabled — credential transitions to `VALIDATION_FAILED`, arming is
  disarmed, `CredentialValidationFailed` is published — test the full
  chain, not just the classification.
- **`ArmingService` expiry**: armed, then simulated time passes
  `expires_at` — `is_armed()` returns `False` without any explicit
  disarm call; a config change on an armed strategy — arming reverts
  to unarmed, requiring re-confirmation.
- **Audit log immutability**: connect as `credential_audit_writer` (and
  separately as the default app role) and attempt `UPDATE`/`DELETE`
  against `credential_audit_log`; assert Postgres itself rejects both —
  same pattern as the AI assistant spec's `test_readonly_role_enforcement.py`,
  inverted for a write-only table.
- **No-plaintext-in-logs test**: run the full decrypt-and-use path with
  `structlog`'s output captured; assert the known plaintext test
  credential value never appears anywhere in the captured log stream.
- **`EmergencyCredentialRevocation`**: trigger it, assert every
  subsequent `CredentialProvider.get_credentials()` call fails until an
  explicit re-grant, and assert an order attempted during revocation
  is rejected before it ever reaches `BinanceExecutionAdapter`.
- **Dual-gate test**: kill switch engaged + arming active — blocked;
  kill switch clear + arming expired — blocked; both clear/active —
  proceeds. All three states tested explicitly, not just the happy path.

## 9. Integration points

- **`BinanceExecutionAdapter` (Stage 2)**: the *only* change is its
  credential source — constructor/call site switches from reading an
  environment variable to calling `CredentialProvider.get_credentials()`.
  Order placement, cancellation, status, and error-handling logic are
  untouched, per the locked-in decision above.
- **Risk Engine `KillSwitch`**: consulted independently alongside
  `ArmingService` — this stage does not modify `KillSwitch`, it adds a
  second, separate gate beside it.
- **Scheduler**: triggers `PermissionValidator`'s recurring re-check and
  `KeyLifecycleManager`'s rotation-due reminders.
- **EventBus**: reused, no new transport.

## 10. Step-by-step build order

1. `KMSClient` interface + `LocalDevKMSClient` + `CredentialVault`
   (encrypt/decrypt round-trip only, no exchange integration yet) +
   round-trip tests.
2. `MainnetGate` + its structural test — build and prove this before
   anything else touches a `mainnet` flag anywhere in the system.
3. `encrypted_credentials` table + `KeyLifecycleManager` (state machine
   only, no validation logic yet) + tests.
4. `credential_audit_log` (INSERT-only role) + `CredentialProvider`
   (decrypt-on-demand + mandatory audit write) + the immutability test
   + the no-plaintext-in-logs test.
5. `PermissionValidator` + scheduled re-check + tests including the
   full disarm-on-withdrawal-detected chain.
6. `arming_state` table + `ArmingService` (expiry, re-confirmation) +
   the dual-gate test against `KillSwitch`.
7. `EmergencyCredentialRevocation` + its tests.
8. Wire `BinanceExecutionAdapter` (Stage 2) to `CredentialProvider` —
   confirm via the existing Stage 2 test suite that order logic is
   provably unchanged, only credential sourcing differs.
9. Update `CLAUDE.md`. State explicitly: this stage is complete, but
   `mainnet=True` should not be used for any real account until a
   deliberate, separate soak period on testnet has run under this full
   security path — that decision is a human one, not something this
   build makes for you.

## 11. Open decisions

1. **KMS provider**: confirmed — interface + `LocalDevKMSClient` only
   for now; no cloud infrastructure exists in this project yet, so a
   real `AWSKMSClient`/`VaultKMSClient` is deferred (unimplemented stub,
   same pattern as Stage 1's `LiveExecutionAdapter`) until real cloud
   infra exists to point it at.
2. **Arming expiry duration**: confirmed — 48 hours, configurable.
3. **Rotation reminder cadence**: confirmed — 90 days.
4. **Soak period before real `mainnet=True` use**: confirmed — this is
   a process commitment, not a code deliverable. Stage 3 shipping and
   passing tests is not the same event as "safe to trade real money";
   that decision follows separately, made by the user.
