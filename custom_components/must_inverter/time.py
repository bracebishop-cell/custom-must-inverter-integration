import contextlib
import logging
from typing import Any
from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import callback
from homeassistant.const import Platform

from .const import DOMAIN, Sensor
from . import MustInverter

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    inverter_data = hass.data[DOMAIN][entry.entry_id]
    inverter = inverter_data["inverter"]
    sensors = inverter_data["sensors"]
    entities = []

    for sensor_info in sensors:
        if sensor_info.platform == Platform.TIME:
            sensor = MustInverterTime(inverter, sensor_info)
            entities.append(sensor)

    async_add_entities(entities)
    return True


class MustInverterTime(TimeEntity):
    def __init__(self, inverter: MustInverter, sensor_info: Sensor):
        """Initialize the sensor."""
        self._inverter = inverter
        self._key = sensor_info.name
        self._address = sensor_info.address

        self._attr_has_entity_name = True
        self._attr_unique_id = f"{self._inverter.data['InverterSerialNumber']}_{self._key}"
        self._attr_translation_key = self._key.lower()
        self._attr_entity_registry_enabled_default = sensor_info.enabled

    async def async_added_to_hass(self):
        self._inverter.async_add_must_inverter_sensor(self._inverter_data_updated)

    async def async_will_remove_from_hass(self) -> None:
        self._inverter.async_remove_must_inverter_sensor(self._inverter_data_updated)

    @callback
    def _inverter_data_updated(self):
        self.async_write_ha_state()

    @property
    def native_value(self):
        if self._key in self._inverter.data:
            return self._inverter.data[self._key]

    async def async_set_value(self, value: time) -> None:
        integer = value.hour * 100 + value.minute

        confirmed = await self._inverter.write_modbus_data(self._address, integer)
        if self._key in self._inverter.data:
            if confirmed == integer:
                self._inverter.data[self._key] = value
            else:
                # the inverter reports something else than what we wrote
                with contextlib.suppress(ValueError):
                    self._inverter.data[self._key] = time(confirmed // 100, confirmed % 100)

    @property
    def device_info(self) -> dict[str, Any] | None:
        return self._inverter._device_info()

    @property
    def available(self) -> dict[str, Any] | None:
        return self._inverter.key_available(self._key)
