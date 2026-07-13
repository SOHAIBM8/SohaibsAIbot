"""
ExecutionAdapter is the ONLY thing that differs between paper and live
(spec decision #1). OrderManager (step 4) is written entirely against
this interface and must never branch on mode='paper' vs 'live'
internally — if it needs to, that's a sign logic leaked out of the
adapter and belongs in a PaperExecutionAdapter/LiveExecutionAdapter
method instead.

Design note (rule 9): the spec lists three methods here, but
submit_order() returns only an Order — there's no way for a caller
(OrderManager, step 4) to learn WHAT an order filled at. Rather than
have OrderManager reach into adapter-specific internals (a real mode
branch, exactly what decision #1 forbids), get_fills() is added as a
fourth interface method both adapters implement identically: paper
returns the fills it simulated, live (Stage 2) will return whatever
the exchange reports. OrderManager can call it uniformly regardless of
mode.
"""

from abc import ABC, abstractmethod

from core.execution.order import Fill, Order


class ExecutionAdapter(ABC):
    @abstractmethod
    def submit_order(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> Order: ...

    @abstractmethod
    def get_order_status(self, client_order_id: str) -> Order: ...

    @abstractmethod
    def get_fills(self, client_order_id: str) -> list[Fill]: ...
