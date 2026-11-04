"""Support for interfacing with Monoprice 6 zone home audio controller."""
import logging
import asyncio
import functools

from serial import SerialException

from homeassistant import core
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PORT, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform, service
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import voluptuous as vol

from .connection import async_monoprice_command
from .const import (
    CONF_SOURCES,
    DOMAIN,
    FIRST_RUN,
    SERVICE_RESTORE,
    SERVICE_SNAPSHOT,
    SERVICE_SET_BALANCE,
    SERVICE_SET_BASS,
    SERVICE_SET_TREBLE,
    ATTR_BALANCE,
    ATTR_BASS,
    ATTR_TREBLE
)

SET_BALANCE_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_BALANCE, default=0): vol.All(int, vol.Range(min=0, max=21))
    }
)

SET_BASS_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_BASS, default=5): vol.All(int, vol.Range(min=0, max=15))
    }
)

SET_TREBLE_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_TREBLE, default=5): vol.All(int, vol.Range(min=0, max=15))
    }
)

_LOGGER = logging.getLogger(__name__)

MAX_VOLUME = 38
PARALLEL_UPDATES = 1


@core.callback
def _get_sources_from_dict(data):
    sources_config = data[CONF_SOURCES]
    source_id_name = {int(index): name for index, name in sources_config.items()}
    source_name_id = {v: k for k, v in source_id_name.items()}
    source_names = sorted(source_name_id.keys(), key=lambda v: source_name_id[v])

    return [source_id_name, source_name_id, source_names]


@core.callback
def _get_sources(config_entry):
    if CONF_SOURCES in config_entry.options:
        data = config_entry.options
    else:
        data = config_entry.data
    return _get_sources_from_dict(data)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Monoprice 6-zone amplifier platform."""

    sources = _get_sources(config_entry)
    port = config_entry.data[CONF_PORT]

    entities = []
    for i in range(1, 4):
        for j in range(1, 7):
            zone_id = (i * 10) + j
            _LOGGER.info("Adding zone %d for port %s", zone_id, port)
            entities.append(
                MonopriceZone(hass, sources, config_entry, zone_id) # Pass hass
            )

    # only call update before add if it's the first run so we can try to detect zones
    first_run = hass.data[DOMAIN][config_entry.entry_id][FIRST_RUN]
    async_add_entities(entities, first_run)

    platform = entity_platform.async_get_current_platform()

    # Rework service handling to be async
    @service.verify_domain_control(DOMAIN)
    async def async_service_handle(service_call: core.ServiceCall) -> None:
        """Handle services to control Monoprice zones."""
        entities = await platform.async_extract_from_service(service_call)

        if not entities:
            _LOGGER.warning("Service call %s did not match any entities", service_call.service)
            return

        tasks = []
        for entity in entities:
            if not isinstance(entity, MonopriceZone):
                _LOGGER.warning("Service call %s targeted non-MonopriceZone entity %s", service_call.service, entity.entity_id)
                continue

            if service_call.service == SERVICE_SNAPSHOT:
                tasks.append(entity.async_snapshot())
            elif service_call.service == SERVICE_RESTORE:
                tasks.append(entity.async_restore())
            elif service_call.service == SERVICE_SET_BALANCE:
                level = int(service_call.data.get(ATTR_BALANCE))
                tasks.append(entity.async_set_balance(level))
            elif service_call.service == SERVICE_SET_BASS:
                level = int(service_call.data.get(ATTR_BASS))
                tasks.append(entity.async_set_bass(level))
            elif service_call.service == SERVICE_SET_TREBLE:
                level = int(service_call.data.get(ATTR_TREBLE))
                tasks.append(entity.async_set_treble(level))

        if tasks:
            await asyncio.gather(*tasks)
        else:
             _LOGGER.warning("No tasks created for service call %s", service_call.service)


    hass.services.async_register(
        DOMAIN,
        SERVICE_SNAPSHOT,
        async_service_handle,
        schema=cv.make_entity_service_schema({}),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE,
        async_service_handle,
        schema=cv.make_entity_service_schema({}),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_BALANCE,
        async_service_handle,
        schema=SET_BALANCE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_BASS,
        async_service_handle,
        schema=SET_BASS_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_TREBLE,
        async_service_handle,
        schema=SET_TREBLE_SCHEMA,
    )

class MonopriceZone(MediaPlayerEntity):
    """Representation of a Monoprice amplifier zone."""
    
    _attr_device_class = MediaPlayerDeviceClass.RECEIVER
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.SELECT_SOUND_MODE
    )
    _attr_has_entity_name = True
    _attr_name = None
    _attr_sound_mode_list = ["Normal", "High Bass", "Medium Bass", "Low Bass"]
    _attr_sound_mode = None

    def __init__(self, hass, sources, config_entry, zone_id): # Add hass
        """Initialize new zone."""
        self.hass = hass # Store hass
        self._config_entry = config_entry # Store config_entry
        # dict source_id -> source name
        self._source_id_name = sources[0]
        # dict source name -> source_id
        self._source_name_id = sources[1]
        # ordered list of all source names
        self._attr_source_list = sources[2]

        self._zone_id = zone_id
        self._attr_unique_id = f"{config_entry.entry_id}_{self._zone_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._zone_id, config_entry.entry_id)},
            manufacturer="Monoprice",
            model="6-Zone Amplifier",
            name=f"Zone {self._zone_id}"
        )
        
        self._snapshot = None
        # self._update_success = True # Removed


    async def _async_monoprice_command(self, method_name: str, *args):
        """Execute a pymonoprice command, marking the zone unavailable if it fails."""
        try:
            return await async_monoprice_command(
                self.hass, self._config_entry, method_name, *args
            )
        except SerialException:
            self._attr_available = False # Mark unavailable immediately
            raise
        except Exception as general_err:
            _LOGGER.error(
                "Unexpected error executing command %s for zone %d: %s",
                method_name, self._zone_id, general_err, exc_info=True
            )
            self._attr_available = False # Mark unavailable immediately
            raise # Re-raise unexpected error


    async def async_update(self) -> None:
        """Retrieve latest state."""
        # Reworked update: uses the command wrapper, handles unavailability
        try:
            state = await self._async_monoprice_command('zone_status', self._zone_id)
            if not state:
                # Should not happen if command succeeded, but check anyway
                _LOGGER.warning("Received no state for zone %d after successful command", self._zone_id)
                self._attr_available = False
                return

            # If command succeeded, entity is available
            self._attr_available = True
            self._attr_state = MediaPlayerState.ON if state.power else MediaPlayerState.OFF
            self._attr_volume_level = state.volume / MAX_VOLUME
            self._attr_is_volume_muted = state.mute
            idx = state.source
            self._attr_source = self._source_id_name.get(idx)
            # TODO: Update other attributes like balance, bass, treble if needed?
            # The pymonoprice library's zone_status might not return everything.
            # Consider adding separate calls if necessary, or enhancing pymonoprice.

        except SerialException:
            # Error handled and logged by _async_monoprice_command, entity marked unavailable
            if self._attr_available: # Only log if previously available
                 _LOGGER.warning("Could not update zone %d, marking unavailable", self._zone_id)
                 self._attr_available = False
        except Exception: # Catch any other unexpected error from the wrapper
             if self._attr_available:
                 _LOGGER.exception("Unexpected error updating zone %d, marking unavailable", self._zone_id)
                 self._attr_available = False


    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return if the entity should be enabled when first added to the entity registry."""
        if(self._zone_id == 10 or self._zone_id == 20 or self._zone_id == 30):
            return False
        # Enable zones 11-16 by default, others based on availability after first update
        # Let HA decide availability based on _attr_available after update runs
        return self._zone_id < 20

    @property
    def media_title(self):
        """Return the current source as medial title."""
        return self.source

    # Convert methods to async and use wrapper
    async def async_snapshot(self):
        """Save zone's current state."""
        try:
            # Note: zone_status might not contain all settings (e.g., bass/treble/balance)
            # Consider enhancing snapshot if needed
            self._snapshot = await self._async_monoprice_command('zone_status', self._zone_id)
            _LOGGER.debug("Snapshot taken for zone %d", self._zone_id)
        except Exception: # Catch errors from wrapper
             _LOGGER.error("Failed to take snapshot for zone %d", self._zone_id)
             self._snapshot = None # Ensure snapshot is cleared on error

    async def async_restore(self):
        """Restore saved state."""
        if self._snapshot:
            try:
                await self._async_monoprice_command('restore_zone', self._snapshot)
                _LOGGER.debug("Snapshot restored for zone %d", self._zone_id)
                # State update will happen on next scheduled update
                self.async_schedule_update_ha_state(True) # Force update after restore
            except Exception: # Catch errors from wrapper
                 _LOGGER.error("Failed to restore snapshot for zone %d", self._zone_id)
        else:
            _LOGGER.warning("No snapshot available to restore for zone %d", self._zone_id)


    async def async_select_source(self, source: str) -> None:
        """Set input source."""
        if source not in self._source_name_id:
            _LOGGER.warning("Invalid source selected: %s for zone %d", source, self._zone_id)
            return
        idx = self._source_name_id[source]
        await self._async_monoprice_command('set_source', self._zone_id, idx)

    async def async_turn_on(self) -> None:
        """Turn the media player on."""
        await self._async_monoprice_command('set_power', self._zone_id, True)

    async def async_turn_off(self) -> None:
        """Turn the media player off."""
        await self._async_monoprice_command('set_power', self._zone_id, False)

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute (true) or unmute (false) media player."""
        await self._async_monoprice_command('set_mute', self._zone_id, mute)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        await self._async_monoprice_command('set_volume', self._zone_id, round(volume * MAX_VOLUME))

    async def async_volume_up(self) -> None:
        """Volume up the media player."""
        # Getting current volume level involves an update cycle or another command.
        # For simplicity and responsiveness, we'll rely on the underlying library's
        # potential internal state or just send the command relative to max.
        # A more robust approach might fetch status first, but could add latency.
        # Let's try sending a relative command if pymonoprice supports it,
        # otherwise calculate based on last known state (less reliable).
        # pymonoprice set_volume is absolute. Calculate based on last known state.
        if self.volume_level is None:
             _LOGGER.warning("Cannot increase volume for zone %d: current volume unknown", self._zone_id)
             return
        current_vol_abs = round(self.volume_level * MAX_VOLUME)
        target_vol_abs = min(current_vol_abs + 1, MAX_VOLUME)
        if target_vol_abs != current_vol_abs: # Avoid sending command if already at max
            await self._async_monoprice_command('set_volume', self._zone_id, target_vol_abs)


    async def async_volume_down(self) -> None:
        """Volume down media player."""
        if self.volume_level is None:
            _LOGGER.warning("Cannot decrease volume for zone %d: current volume unknown", self._zone_id)
            return
        current_vol_abs = round(self.volume_level * MAX_VOLUME)
        target_vol_abs = max(current_vol_abs - 1, 0)
        if target_vol_abs != current_vol_abs: # Avoid sending command if already at min
            await self._async_monoprice_command('set_volume', self._zone_id, target_vol_abs)

    # Service call methods - now async and accept level directly
    async def async_set_balance(self, level: int) -> None:
        """Set balance level."""
        await self._async_monoprice_command('set_balance', self._zone_id, level)

    async def async_set_bass(self, level: int) -> None:
        """Set bass level."""
        await self._async_monoprice_command('set_bass', self._zone_id, level)

    async def async_set_treble(self, level: int) -> None:
        """Set treble level."""
        await self._async_monoprice_command('set_treble', self._zone_id, level)

    async def async_select_sound_mode(self, sound_mode) -> None:
        """Switch the sound mode of the entity."""
        # Note: This directly sets bass level based on mode name.
        # Consider if a dedicated sound_mode command exists or is needed in pymonoprice.
        self._sound_mode = sound_mode # Update internal state immediately
        self.async_write_ha_state() # Reflect change in HA state

        level = 7 # Default to Normal
        if(sound_mode == "High Bass"):
            level = 12
        elif(sound_mode == "Medium Bass"):
            level = 10
        elif(sound_mode == "Low Bass"):
            level = 3
        elif(sound_mode != "Normal"):
             _LOGGER.warning("Unsupported sound mode selected: %s for zone %d", sound_mode, self._zone_id)
             return # Don't send command for unsupported modes

        await self.async_set_bass(level) # Use the async bass setting method
