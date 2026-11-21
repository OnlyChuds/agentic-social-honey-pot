from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

ClockFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


class RateGovernor:
    """Async token-bucket rate limiter + concurrency semaphore for the Target API."""

    def __init__(
        self,
        *,
        req_per_min: int = 50,
        max_parallel: int = 5,
        clock: ClockFn = time.monotonic,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        if req_per_min <= 0:
            raise ValueError("req_per_min must be > 0")
        if max_parallel <= 0:
            raise ValueError("max_parallel must be > 0")

        self.req_per_min = req_per_min
        self.max_parallel = max_parallel
        self._capacity = float(req_per_min)
        self._refill_rate = float(req_per_min) / 60.0  # tokens per second
        self._tokens = float(req_per_min)
        self._clock = clock
        self._sleep = sleep
        self._last_refill = clock()
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_parallel)

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        if elapsed > 0.0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
            self._last_refill = now

    async def _take_token(self) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                needed = 1.0 - self._tokens
                wait_s = needed / self._refill_rate
            await self._sleep(wait_s)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        await self._semaphore.acquire()
        try:
            await self._take_token()
            yield
        finally:
            self._semaphore.release()
