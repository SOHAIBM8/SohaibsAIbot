"""
STAGE 2 STUB. Do not implement submit_order/cancel_order/get_order_status
logic here in Stage 1 — this class exists only so OrderManager can be
written against the full ExecutionAdapter interface now, and so the
interface itself can be proven adapter-agnostic before a second
implementation exists. Every method raises NotImplementedError,
pointing at the (not-yet-written) Stage 2 spec — this is intentional,
not an oversight or an incomplete implementation.

Stage 1 ships with zero exchange authentication and zero real order
placement (spec decision #5). Do not add API key handling, HTTP calls,
or any exchange-specific logic to this file until Stage 2 is specced
and approved.
"""

from core.execution.execution_adapter import ExecutionAdapter
from core.execution.order import Fill, Order

_STAGE_2_MESSAGE = (
    "LiveExecutionAdapter is a Stage 1 interface stub — real order placement "
    "is Stage 2 work and is not yet specced or approved. See "
    "docs/execution_engine_stage1_spec.md decision #5."
)


class LiveExecutionAdapter(ExecutionAdapter):
    def submit_order(self, order: Order) -> Order:
        raise NotImplementedError(_STAGE_2_MESSAGE)

    def cancel_order(self, client_order_id: str) -> Order:
        raise NotImplementedError(_STAGE_2_MESSAGE)

    def get_order_status(self, client_order_id: str) -> Order:
        raise NotImplementedError(_STAGE_2_MESSAGE)

    def get_fills(self, client_order_id: str) -> list[Fill]:
        raise NotImplementedError(_STAGE_2_MESSAGE)
