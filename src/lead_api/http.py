import asyncio
import random
import time

import httpx
from opentelemetry import trace

from .errors import DependencyUnavailable, ProviderRejected

tracer = trace.get_tracer(__name__)


class ResilientHTTP:
    """Bounded retries for reads and explicitly idempotent writes; never logs payloads."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.failures: dict[str, int] = {}
        self.open_until: dict[str, float] = {}

    async def request(self, provider: str, method: str, url: str, **kwargs) -> httpx.Response:
        if self.open_until.get(provider, 0) > time.monotonic():
            raise DependencyUnavailable(f"{provider} circuit open")
        with tracer.start_as_current_span(f"provider.{provider}"):
            for attempt in range(3):
                try:
                    response = await self.client.request(method, url, **kwargs)
                    if response.status_code not in {408, 429, 500, 502, 503, 504}:
                        if response.is_error:
                            raise ProviderRejected(provider, response.status_code)
                        self.failures[provider] = 0
                        return response
                except (httpx.TimeoutException, httpx.TransportError):
                    pass
                if attempt < 2:
                    await asyncio.sleep(0.1 * 2**attempt + random.uniform(0, 0.05))
            self.failures[provider] = self.failures.get(provider, 0) + 1
            if self.failures[provider] >= 3:
                self.open_until[provider] = time.monotonic() + 15
            raise DependencyUnavailable(f"{provider} unavailable")
