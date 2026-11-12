"""Which optional modules the tracker has (GET /api/modules): the sensor library's tools only show with a module
that decodes buses (the logic analyzer). Its data stays in the api either way."""

from __future__ import annotations

import time

from .client import Api, ApiError

BUS_MODULES = {"analyzer"}  # modules that decode I2C / SPI
CACHE_S = 5.0


class Modules:
    def __init__(self, api: Api):
        self.api = api
        self._cached: tuple[float, bool] | None = None

    async def bus_decoding(self) -> bool:
        """Is a bus-decoding module installed (cached briefly; unreachable api = no)."""
        now = time.monotonic()
        if self._cached is None or now - self._cached[0] > CACHE_S:
            try:
                mods = await self.api.get("/api/modules")
                on = any(m.get("name") in BUS_MODULES and m.get("installed") for m in mods)
            except ApiError:
                on = False
            self._cached = (now, on)
        return self._cached[1]
