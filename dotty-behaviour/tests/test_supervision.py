import asyncio
from types import SimpleNamespace

from routes.health import health
from supervision import supervise_consumer


class _CrashOnce:
    def __init__(self) -> None:
        self.calls = 0
        self.recovered = asyncio.Event()

    async def run(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        self.recovered.set()
        await asyncio.Future()


def test_supervisor_marks_crash_and_restarts_consumer() -> None:
    async def go() -> None:
        consumer = _CrashOnce()
        status: dict = {}
        task = asyncio.create_task(
            supervise_consumer(consumer, status, restart_delay_sec=0.001)
        )
        try:
            await asyncio.sleep(0)
            assert status["state"] == "dead"
            assert status["restarts"] == 1
            await asyncio.wait_for(consumer.recovered.wait(), timeout=1)
            assert status["state"] == "running"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


def test_disabled_consumer_is_not_restarted() -> None:
    class Disabled:
        enabled = False

        async def run(self) -> None:
            raise AssertionError("disabled consumer must not run")

    async def go() -> None:
        status: dict = {}
        await supervise_consumer(Disabled(), status)
        assert status == {"state": "disabled", "name": "Disabled"}

    asyncio.run(go())


def test_health_reports_dead_consumer_as_service_unhealthy() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                consumer_status={
                    "FaceGreeter": {
                        "name": "FaceGreeter",
                        "state": "dead",
                        "error": "boom",
                    }
                },
                consumer_tasks=[],
            )
        )
    )
    response = asyncio.run(health(request))
    assert response.status_code == 503
    assert response.body and b"FaceGreeter" in response.body
