"""
Decision #6: mainnet credentials structurally require the real KMS
path — LocalDevKMSClient is code-level INCAPABLE of being used when
mainnet=True, not merely discouraged by convention or a warning log
line. This is the first thing in the system to introduce a `mainnet`
flag at all (CredentialVault, step 1, has none) — every later
component that accepts one (KeyLifecycleManager, CredentialProvider,
ArmingState) must route through MainnetGate.check() before doing
anything with a mainnet=True request, not just this module.

MainnetGate.check() takes the KMSClient INSTANCE, not its class name
as a string or any other spoofable signal — isinstance() against the
concrete LocalDevKMSClient type is the actual, structural check
decision #6 asks for.
"""

from core.security.kms_client import KMSClient, LocalDevKMSClient


class MainnetGateViolationError(RuntimeError):
    """Raised — never a warning — when mainnet=True is paired with a
    KMS client that is not a real, external-KMS-backed implementation."""


class MainnetGate:
    @staticmethod
    def check(mainnet: bool, kms_client: KMSClient) -> None:
        if mainnet and isinstance(kms_client, LocalDevKMSClient):
            raise MainnetGateViolationError(
                f"mainnet=True credentials require a real KMS-backed client; "
                f"{type(kms_client).__name__} is testnet-only and structurally "
                "rejected (docs/execution_engine_stage3_spec.md decision #6)"
            )
