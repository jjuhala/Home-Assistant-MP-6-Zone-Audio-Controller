"""Support for interfacing with Monoprice 6 zone home audio controller."""
from code import interact
import logging

from serial import SerialException

from homeassistant import core
try:
    from homeassistant.components.number import (
        NumberEntity as NumberEntity,
    )
except ImportError:
    from homeassistant.components.number import NumberEntity

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform, service
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .connection import async_monoprice_command
from .const import (
    DOMAIN,
    FIRST_RUN
)

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Monoprice 6-zone amplifier platform."""
    port = config_entry.data[CONF_PORT]

    entities = []
    for i in range(1, 4):
        for j in range(1, 7):
            zone_id = (i * 10) + j
            _LOGGER.info("Adding number entities for zone %d for port %s", zone_id, port)
            entities.append(MonopriceZone("Balance", config_entry, zone_id))
            entities.append(MonopriceZone("Bass", config_entry, zone_id))
            entities.append(MonopriceZone("Treble", config_entry, zone_id))

    # only call update before add if it's the first run so we can try to detect zones
    first_run = hass.data[DOMAIN][config_entry.entry_id][FIRST_RUN]
    async_add_entities(entities, first_run)

    platform = entity_platform.async_get_current_platform()

    @service.verify_domain_control(hass, DOMAIN)
    async def async_service_handle(service_call: core.ServiceCall) -> None:
        """Handle for services."""
        entities = await platform.async_extract_from_service(service_call)

        if not entities:
            return

class MonopriceZone(NumberEntity):
    """Representation of a Monoprice amplifier zone."""

    def __init__(self, control_type, config_entry, zone_id):
        """Initialize new zone controls."""
        self._config_entry = config_entry
        self._control_type = control_type
        self._zone_id = zone_id
        
        self._attr_unique_id = f"{config_entry.entry_id}_{self._zone_id}_{self._control_type}"
        self._attr_has_entity_name = True
        self._attr_name = f"{control_type} level"
        self._attr_native_step = 1
        self._attr_native_value = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._zone_id, config_entry.entry_id)},
            manufacturer="Monoprice",
            model="6-Zone Amplifier",
            name=f"Zone {self._zone_id}"
        )

        if(control_type == "Balance"):
            self._attr_native_min_value = 0
            self._attr_native_max_value = 20
            self._attr_icon = "mdi:scale-balance"
        elif(control_type == "Bass"):
            self._attr_native_min_value = -7
            self._attr_native_max_value = 14
            self._attr_icon = "mdi:speaker"
        elif(control_type == "Treble"):
            self._attr_native_min_value = -7
            self._attr_native_max_value = 14
            self._attr_icon = "mdi:surround-sound"
            
        self._update_success = True
        
    async def async_update(self) -> None:
        """Retrieve latest value."""
        try:
            state = await async_monoprice_command(
                self.hass, self._config_entry, "zone_status", self._zone_id
            )
        except SerialException:
            self._update_success = False
            self._attr_available = False
            return

        if not state:
            self._update_success = False
            return

        self._attr_available = True
        if(self._control_type == "Balance"):
            self._attr_native_value = state.balance
        elif(self._control_type == "Bass"):
            self._attr_native_value = state.bass
        elif(self._control_type == "Treble"):
            self._attr_native_value = state.treble

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return if the entity should be enabled when first added to the entity registry."""
        if(self._zone_id == 10 or self._zone_id == 20 or self._zone_id == 30):
            return False
        return self._zone_id < 20 or self._update_success

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        if(self._control_type == "Balance"):
            command = "set_balance"
        elif(self._control_type == "Bass"):
            command = "set_bass"
        elif(self._control_type == "Treble"):
            command = "set_treble"
        await async_monoprice_command(
            self.hass, self._config_entry, command, self._zone_id, int(value)
        )
