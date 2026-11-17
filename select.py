from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import HondaLinkDataUpdateCoordinator
from .entity import HondaLinkEntity, entity_wanted
from .util import CLIMATE_OFF, REMOTE_START_CLIMATE_OPTIONS

REMOTE_START_CLIMATE_KEY = "remote_start_climate"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    if entity_wanted(hass, entry, REMOTE_START_CLIMATE_KEY):
        async_add_entities([HondaLinkRemoteStartClimateSelect(coordinator, entry)])


class HondaLinkRemoteStartClimateSelect(HondaLinkEntity, SelectEntity, RestoreEntity):
    """What the car's climate system does on a remote start sent from Home Assistant.

    The choice is kept in Home Assistant (the car doesn't report it) and is sent with the
    Start engine and Extend remote start commands. "off" sends no climate block.
    """

    _attr_translation_key = REMOTE_START_CLIMATE_KEY
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:air-conditioner"
    _attr_options = list(REMOTE_START_CLIMATE_OPTIONS)

    def __init__(self, coordinator: HondaLinkDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, REMOTE_START_CLIMATE_KEY)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in REMOTE_START_CLIMATE_OPTIONS:
            self.coordinator.remote_start_climate = last.state

    @property
    def current_option(self) -> str:
        option = self.coordinator.remote_start_climate
        return option if option in REMOTE_START_CLIMATE_OPTIONS else CLIMATE_OFF

    async def async_select_option(self, option: str) -> None:
        self.coordinator.remote_start_climate = option
        self.async_write_ha_state()
