import asyncio

import pytest

from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_runtime_status_heartbeat_refreshes_running_status():
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    calls = []

    def record_status(gateway_state=None, exit_reason=None):
        calls.append((gateway_state, exit_reason))
        runner._running = False
        runner._shutdown_event.set()

    runner._update_runtime_status = record_status

    await asyncio.wait_for(
        runner._runtime_status_heartbeat_watcher(interval=0.01),
        timeout=1,
    )

    assert calls == [("running", None)]
