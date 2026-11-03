"""Shared access to the Monoprice amplifier connection.

Every platform must send its commands through async_monoprice_command and must not
keep its own reference to the Monoprice object. A reconnect replaces that object in
hass.data, so a platform holding on to the old one polls a dead connection forever.
"""
import logging
import time

from pymonoprice import get_monoprice
from serial import SerialException

from homeassistant.const import CONF_PORT

from .const import DOMAIN, MONOPRICE_LOCK, MONOPRICE_OBJECT

_LOGGER = logging.getLogger(__name__)

# Seconds to wait after a failed recovery before trying again. The platforms poll about
# 42 times per 30 s, and each attempt holds the lock for up to 7 s during an outage.
RECONNECT_BACKOFF = 30

# hass.data key: when the next recovery attempt is due. Absent while the connection works.
_RETRY_AT = "retry_at"


def _close_connection(monoprice) -> None:
    """Close the port of a replaced connection, so it doesn't linger on the bridge."""
    port = getattr(monoprice, "_port", None)
    if port is None:
        return
    try:
        port.close()
    except Exception as err:  # A failed close must not stop the reconnect
        _LOGGER.debug("Could not close old Monoprice connection: %s", err)


async def async_monoprice_command(hass, config_entry, method_name: str, *args):
    """Run a pymonoprice command, reconnecting and retrying once if the connection died.

    Raises SerialException if the command fails, right away while waiting out the backoff.
    An outage logs one warning and one info line. Callers should not log each failure.
    A reconnect whose retry also fails counts as failed: with the bridge up and the
    amplifier off, connecting works but nothing ever answers.
    """
    data = hass.data[DOMAIN][config_entry.entry_id]
    port = config_entry.data[CONF_PORT]

    async with data[MONOPRICE_LOCK]:
        retry_at = data.get(_RETRY_AT)

        if retry_at is None:
            try:
                return await hass.async_add_executor_job(
                    getattr(data[MONOPRICE_OBJECT], method_name), *args
                )
            except SerialException as err:
                _LOGGER.warning(
                    "Lost connection to Monoprice amplifier at %s: %s", port, err
                )
        elif time.monotonic() < retry_at:
            raise SerialException(f"Connection to Monoprice amplifier at {port} is down")

        await hass.async_add_executor_job(_close_connection, data[MONOPRICE_OBJECT])
        try:
            data[MONOPRICE_OBJECT] = await hass.async_add_executor_job(get_monoprice, port)
            result = await hass.async_add_executor_job(
                getattr(data[MONOPRICE_OBJECT], method_name), *args
            )
        except Exception as err:
            data[_RETRY_AT] = time.monotonic() + RECONNECT_BACKOFF
            _LOGGER.debug("Could not restore Monoprice connection at %s: %s", port, err)
            raise

        data.pop(_RETRY_AT, None)
        _LOGGER.info("Connection to Monoprice amplifier at %s restored", port)
        return result
