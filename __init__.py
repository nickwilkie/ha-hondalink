from __future__ import annotations

import logging
import uuid

import aiohttp

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import HondaLinkAPI, HondaLinkError
from .const import (
    ATTR_AUTO_EXTEND,
    ATTR_CLIMATE,
    ATTR_STOP_AFTER,
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_REG_KEY,
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_EXPIRES_AT,
    CONF_FEATURES,
    CONF_HIDAS_IDENT,
    CONF_LANGUAGE,
    CONF_LOCK_COMMAND,
    CONF_PASSWORD,
    CONF_PIN,
    CONF_REFRESH_TOKEN,
    CONF_SESSION_ID,
    CONF_UNLOCK_COMMAND,
    CONF_VIN,
    DATA_API,
    DATA_COORDINATOR,
    DATA_FEATURES,
    DEFAULT_LOCK_COMMAND,
    DEFAULT_UNLOCK_COMMAND,
    DOMAIN,
    SERVICE_REMOTE_START,
)
from .coordinator import HondaLinkDataUpdateCoordinator
from .util import REMOTE_START_CLIMATE_OPTIONS, REMOTE_START_MAX_MINUTES, feature_ids

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.LOCK,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.DEVICE_TRACKER,
]


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

REMOTE_START_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE_ID): cv.string,
        # YAML turns a bare 72 into a number, so accept that for the temperatures.
        vol.Optional(ATTR_CLIMATE): vol.All(vol.Coerce(str), vol.Lower, vol.In(REMOTE_START_CLIMATE_OPTIONS)),
        vol.Optional(ATTR_AUTO_EXTEND, default=False): cv.boolean,
        vol.Optional(ATTR_STOP_AFTER): vol.All(vol.Coerce(int), vol.Range(min=1, max=REMOTE_START_MAX_MINUTES)),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def remote_start(call: ServiceCall) -> None:
        coordinator = _coordinator_for_call(hass, call)
        await coordinator.async_remote_start(
            climate=call.data.get(ATTR_CLIMATE),
            auto_extend=call.data[ATTR_AUTO_EXTEND],
            stop_after=call.data.get(ATTR_STOP_AFTER),
        )

    hass.services.async_register(DOMAIN, SERVICE_REMOTE_START, remote_start, schema=REMOTE_START_SCHEMA)
    return True


def _coordinator_for_call(hass: HomeAssistant, call: ServiceCall) -> HondaLinkDataUpdateCoordinator:
    """The car a service call is for: the given device, or the only car set up."""
    coordinators = [data[DATA_COORDINATOR] for data in hass.data.get(DOMAIN, {}).values()]
    device_id = call.data.get(ATTR_DEVICE_ID)
    if device_id:
        device = dr.async_get(hass).async_get(device_id)
        vins = {identifier[1] for identifier in device.identifiers if identifier[0] == DOMAIN} if device else set()
        for coordinator in coordinators:
            if coordinator.vin in vins:
                return coordinator
        raise ServiceValidationError(f"{device_id} is not a HondaLink vehicle that is set up")
    if len(coordinators) == 1:
        return coordinators[0]
    if not coordinators:
        raise ServiceValidationError("No HondaLink vehicle is set up")
    raise ServiceValidationError("More than one HondaLink vehicle is set up: choose one with device_id")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    data = dict(entry.data)
    changed = False
    if not data.get(CONF_DEVICE_ID):
        data[CONF_DEVICE_ID] = str(uuid.uuid4())
        changed = True
    if not data.get(CONF_SESSION_ID):
        data[CONF_SESSION_ID] = str(uuid.uuid4())
        changed = True
    if changed:
        hass.config_entries.async_update_entry(entry, data=data)

    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    api = HondaLinkAPI(
        session,
        email=data[CONF_EMAIL],
        password=data[CONF_PASSWORD],
        pin=entry.options.get(CONF_PIN, data.get(CONF_PIN)),
        vin=data[CONF_VIN],
        client_reg_key=data.get(CONF_CLIENT_REG_KEY),
        access_token=data.get(CONF_ACCESS_TOKEN),
        refresh_token=data.get(CONF_REFRESH_TOKEN),
        expires_at=data.get(CONF_EXPIRES_AT),
        country=data.get(CONF_COUNTRY, "US"),
        language=data.get(CONF_LANGUAGE, "en"),
        hidas_ident=data.get(CONF_HIDAS_IDENT),
        device_id=data[CONF_DEVICE_ID],
        session_id=data[CONF_SESSION_ID],
        lock_command=entry.options.get(CONF_LOCK_COMMAND, DEFAULT_LOCK_COMMAND),
        unlock_command=entry.options.get(CONF_UNLOCK_COMMAND, DEFAULT_UNLOCK_COMMAND),
    )
    await api.async_ensure_login()

    auth_data = api.export_auth_data()
    if any(data.get(key) != value for key, value in auth_data.items()):
        hass.config_entries.async_update_entry(entry, data={**data, **auth_data})

    features = await _async_load_features(hass, entry, api)

    coordinator = HondaLinkDataUpdateCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_API: api,
        DATA_COORDINATOR: coordinator,
        DATA_FEATURES: features,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_load_features(hass: HomeAssistant, entry: ConfigEntry, api: HondaLinkAPI) -> set[str] | None:
    """The car's feature IDs, used to create only the entities it supports.

    Fetched fresh at each setup and saved in the entry. If Honda doesn't answer, the saved
    list is used; with no list at all this returns None and every entity is created.
    """
    try:
        features = feature_ids(await api.async_get_feature_list())
    except (HondaLinkError, aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("HondaLink feature list request failed: %s", err)
        features = None
    if features is None:
        saved = entry.data.get(CONF_FEATURES)
        return set(saved) if saved else None
    if sorted(features) != entry.data.get(CONF_FEATURES):
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_FEATURES: sorted(features)})
    return features


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data:
            data[DATA_COORDINATOR].cancel_remote_start_schedule()
    return unload_ok
