"""
Confirms the ExecutionAdapter abstraction is real (LiveExecutionAdapter
genuinely implements it, proving OrderManager can be written against
the interface alone) without any Stage 2 order-placement logic being
smuggled in — every method must raise NotImplementedError, not silently
succeed or return a fake order.
"""

import pytest

from core.execution.execution_adapter import ExecutionAdapter
from core.execution.live_execution_adapter import LiveExecutionAdapter


def test_execution_adapter_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        ExecutionAdapter()  # abstract methods unimplemented


def test_live_execution_adapter_is_an_execution_adapter():
    adapter = LiveExecutionAdapter()
    assert isinstance(adapter, ExecutionAdapter)


def test_submit_order_raises_not_implemented():
    adapter = LiveExecutionAdapter()
    with pytest.raises(NotImplementedError, match="Stage 2"):
        adapter.submit_order(order=None)


def test_cancel_order_raises_not_implemented():
    adapter = LiveExecutionAdapter()
    with pytest.raises(NotImplementedError, match="Stage 2"):
        adapter.cancel_order(client_order_id="co-1")


def test_get_order_status_raises_not_implemented():
    adapter = LiveExecutionAdapter()
    with pytest.raises(NotImplementedError, match="Stage 2"):
        adapter.get_order_status(client_order_id="co-1")


def test_get_fills_raises_not_implemented():
    adapter = LiveExecutionAdapter()
    with pytest.raises(NotImplementedError, match="Stage 2"):
        adapter.get_fills(client_order_id="co-1")
