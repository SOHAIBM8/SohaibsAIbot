import time

from core.ingestion.event_bus import PostgresEventBus
from core.ingestion.events import CandlesIngested


def test_postgres_event_bus_delivers_published_events_to_subscribers():
    bus = PostgresEventBus()
    received = []
    bus.subscribe("CandlesIngested", received.append)
    bus.start()
    try:
        event = CandlesIngested(
            exchange="fake", symbol="BTC/USDT", timeframe="1h", count=5, run_id=1
        )
        bus.publish(event)

        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            time.sleep(0.05)

        assert len(received) == 1
        assert received[0]["exchange"] == "fake"
        assert received[0]["count"] == 5
    finally:
        bus.close()


def test_postgres_event_bus_does_not_deliver_to_unsubscribed_channel():
    bus = PostgresEventBus()
    received = []
    bus.subscribe("GapRepaired", received.append)
    bus.start()
    try:
        bus.publish(CandlesIngested(exchange="fake", symbol="BTC/USDT", timeframe="1h", count=1))
        time.sleep(0.5)
        assert received == []
    finally:
        bus.close()
