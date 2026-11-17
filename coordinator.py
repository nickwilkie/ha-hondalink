from __future__ import annotations

from datetime import timedelta
import logging
import time
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HondaLinkAPI, HondaLinkCommandError, HondaLinkError
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_VIN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ENROLLMENT_CHECK_INTERVAL,
    RECALL_CHECK_INTERVAL,
)
from .util import (
    CLIMATE_OFF,
    enrollment_status,
    open_recalls,
    recall_campaigns,
    remote_engine_running,
    remote_start_schedule,
    remote_start_vehicle_control,
    status_body,
)

# Errors from the slow side checks (enrollment, recalls). They are logged and the last
# known value is kept; they never fail the dashboard update.
_SIDE_CHECK_ERRORS = (HondaLinkError, aiohttp.ClientError, TimeoutError)

_LOGGER = logging.getLogger(__name__)


class HondaLinkDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: HondaLinkAPI) -> None:
        self.api = api
        self.vin = entry.data[CONF_VIN]
        self.enrollment_status: str | None = None
        self.open_recalls: int | None = None
        self.recalls: list[dict[str, Any]] = []
        # Chosen with the "Remote start climate" select; sent with every remote start.
        self.remote_start_climate: str = CLIMATE_OFF
        # Pending automatic extension / stop of the remote start in progress, and the
        # climate mode that start was sent with.
        self._remote_start_timers: list[CALLBACK_TYPE] = []
        self._remote_start_mode: str = CLIMATE_OFF
        self._checked_at: dict[str, float] = {}
        interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            logging.getLogger(__name__),
            name=f"{DOMAIN}-{self.vin}",
            update_interval=timedelta(minutes=interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.api.async_get_dashboard_latest(self.vin)
        except HondaLinkError as err:
            raise UpdateFailed(str(err)) from err
        await self._async_check_enrollment()
        await self._async_check_recalls()
        return data

    def _due(self, check: str, interval: timedelta) -> bool:
        # Slow-changing data is checked every few hours rather than on every poll.
        now = time.monotonic()
        last = self._checked_at.get(check)
        if last is not None and now - last < interval.total_seconds():
            return False
        self._checked_at[check] = now
        return True

    async def _async_check_enrollment(self) -> None:
        if not self._due("enrollment", ENROLLMENT_CHECK_INTERVAL):
            return
        try:
            # Keep only the status: the rest of the reply is the owner's personal details.
            self.enrollment_status = enrollment_status(await self.api.async_get_vehicle_entitlement(self.vin))
        except _SIDE_CHECK_ERRORS as err:
            _LOGGER.debug("HondaLink enrollment check failed: %s", err)

    async def _async_check_recalls(self) -> None:
        if not self._due("recalls", RECALL_CHECK_INTERVAL):
            return
        try:
            data = await self.api.async_get_recalls(self.vin)
        except _SIDE_CHECK_ERRORS as err:
            _LOGGER.debug("Honda recall lookup failed: %s", err)
            return
        count = open_recalls(data)
        if count is None:
            _LOGGER.debug("Honda recall lookup gave no usable answer")
            return
        self.open_recalls = count
        self.recalls = recall_campaigns(data)

    async def async_force_refresh(self) -> None:
        try:
            await self.api.async_request_dashboard_update(self.vin)
        except HondaLinkCommandError as err:
            _LOGGER.warning("HondaLink live dashboard refresh request failed: %s", err)
        except HondaLinkError as err:
            _LOGGER.warning("HondaLink live dashboard refresh request failed: %s", err)
        await self.async_refresh()

    async def async_start_engine(self) -> None:
        await self.async_remote_start()

    async def async_remote_start(
        self,
        *,
        climate: str | None = None,
        auto_extend: bool = False,
        stop_after: float | None = None,
    ) -> None:
        """Remote start, optionally followed by an automatic extension and/or stop.

        climate defaults to the "Remote start climate" select. The follow-ups are timers in
        Home Assistant (see util.remote_start_schedule); a later start or stop from Home
        Assistant, or a restart of Home Assistant, cancels them.
        """
        mode = climate if climate is not None else self.remote_start_climate
        vehicle_control = remote_start_vehicle_control(mode)
        extend_at, stop_at = remote_start_schedule(auto_extend, stop_after)
        self.cancel_remote_start_schedule()
        await self._run_command(lambda: self.api.async_start_engine(vehicle_control=vehicle_control))
        self._remote_start_mode = mode
        if extend_at is not None:
            self._remote_start_timers.append(async_call_later(self.hass, extend_at * 60, self._async_auto_extend))
        if stop_at is not None:
            self._remote_start_timers.append(async_call_later(self.hass, stop_at * 60, self._async_auto_stop))

    @callback
    def cancel_remote_start_schedule(self) -> None:
        while self._remote_start_timers:
            self._remote_start_timers.pop()()

    async def _async_auto_extend(self, _now=None) -> None:
        # Only extend a remote start that is still running: if someone has stopped the car
        # or driven off in the meantime, don't risk starting it again.
        await self.async_force_refresh()
        if remote_engine_running(status_body(self.data or {})) is not True:
            _LOGGER.info("HondaLink remote start is no longer running; not extending it")
            self.cancel_remote_start_schedule()
            return
        try:
            await self._run_command(
                lambda: self.api.async_start_engine(
                    extend=True, vehicle_control=remote_start_vehicle_control(self._remote_start_mode)
                )
            )
        except HomeAssistantError as err:
            _LOGGER.warning("HondaLink automatic remote start extension failed: %s", err)

    async def _async_auto_stop(self, _now=None) -> None:
        self._remote_start_timers.clear()
        try:
            await self._run_command(self.api.async_stop_engine)
        except HomeAssistantError as err:
            _LOGGER.warning("HondaLink automatic engine stop failed: %s", err)

    async def async_extend_engine(self) -> None:
        # The app's "add 10 minutes": the start command with extend set. The car allows one
        # extension of a running remote start. Like the app, it carries the climate block.
        await self._run_command(
            lambda: self.api.async_start_engine(
                extend=True, vehicle_control=remote_start_vehicle_control(self.remote_start_climate)
            )
        )

    async def async_stop_engine(self) -> None:
        self.cancel_remote_start_schedule()
        await self._run_command(self.api.async_stop_engine)

    async def async_lock(self) -> None:
        await self._run_command(self.api.async_lock)

    async def async_unlock(self) -> None:
        await self._run_command(self.api.async_unlock)

    async def async_horn(self) -> None:
        await self._run_command(self.api.async_horn)

    async def async_lights(self) -> None:
        await self._run_command(self.api.async_lights)

    async def async_stop_horn_lights(self) -> None:
        await self._run_command(self.api.async_stop_horn_lights)

    async def _run_command(self, command) -> None:
        try:
            await command()
        except HondaLinkCommandError as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()
