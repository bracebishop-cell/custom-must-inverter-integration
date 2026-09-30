"""Entity level behaviour (availability, verified writes, safe enum lookup)."""

from datetime import time

import pytest

from custom_components.must_inverter import STALE_AFTER_FAILURES
from custom_components.must_inverter import number as number_platform
from custom_components.must_inverter import select as select_platform
from custom_components.must_inverter import sensor as sensor_platform
from custom_components.must_inverter import switch as switch_platform
from custom_components.must_inverter import time as time_platform
from custom_components.must_inverter.const import get_sensors_for_model
from tests.conftest import HomeAssistantError, ServiceValidationError


def info(name, model="pv1800"):
    return next(s for s in get_sensors_for_model(model) if s.name == name)


@pytest.fixture
async def polled(inverter):
    inverter._client.values.update({15215: 48, 15216: 1000})  # 48 V battery grade, rated current
    assert await inverter.read_modbus_data() is True
    inverter._client.reads.clear()
    return inverter


async def test_sensor_scaling_and_enum(polled):
    voltage = sensor_platform.MustInverterSensor(polled, info("BatteryVoltage"))
    polled.data["BatteryVoltage"] = 573
    assert voltage.state == 57.3

    state = sensor_platform.MustInverterSensor(polled, info("ChargerWorkstate"))
    polled.data["ChargerWorkstate"] = 2
    assert state.state == "work"
    polled.data["ChargerWorkstate"] = -1  # used to wrap around to the last option
    assert state.state is None
    polled.data["ChargerWorkstate"] = 99  # used to raise IndexError
    assert state.state is None


async def test_sensor_becomes_unavailable_when_stale(polled):
    voltage = sensor_platform.MustInverterSensor(polled, info("BatteryVoltage"))
    assert voltage.available is True

    polled._client.fail_starts.add(15201)
    for _ in range(STALE_AFTER_FAILURES):
        await polled._async_refresh_modbus_data()

    assert voltage.available is False


async def test_number_write_success_updates_state(polled):
    entity = number_platform.MustInverterNumber(polled, info("FloatVoltage"))
    await entity.async_set_native_value(54.4)
    assert polled._client.writes == [(10103, 544)]
    assert polled.data["FloatVoltage"] == 544
    assert entity.state == 54.4


async def test_number_out_of_range_is_never_sent(polled):
    entity = number_platform.MustInverterNumber(polled, info("FloatVoltage"))
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(70.0)  # far above the 58.4 V ceiling for a 48 V bank
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(10.0)
    assert polled._client.writes == []


async def test_number_failed_write_does_not_change_state(polled):
    entity = number_platform.MustInverterNumber(polled, info("FloatVoltage"))
    before = polled.data["FloatVoltage"]
    polled._client.reject_writes = True
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(54.4)
    assert polled.data["FloatVoltage"] == before


async def test_select_write_and_unknown_value(polled):
    entity = select_platform.MustInverterSelect(polled, info("BatteryType"))
    await entity.async_select_option("lithium")
    assert polled._client.writes == [(10110, 2)]
    assert entity.current_option == "lithium"
    polled.data["BatteryType"] = 500
    assert entity.current_option is None


async def test_time_entity_native_value_and_write(no_sleep):
    from tests.conftest import make_inverter

    inverter = make_inverter("ph1100")  # time entities only exist on this model
    assert await inverter.read_modbus_data() is True
    entity = time_platform.MustInverterTime(inverter, info("BatteryEqualizationStartTime", "ph1100"))
    inverter.data["BatteryEqualizationStartTime"] = time(1, 0)

    await entity.async_set_value(time(13, 45))

    assert inverter._client.writes == [(10118, 1345)]
    assert entity.native_value == time(13, 45)


async def test_settings_switch_reads_fresh_and_preserves_other_bits(polled):
    setting = switch_platform.Setting(4, "GridBuzz", False, True)  # bit 4, not flipped
    entity = switch_platform.MustInverterSettingsSwitch(polled, setting)
    polled.data["SystemSetting"] = 0  # stale cache
    polled._client.values[20142] = 0b0000_0011  # what the inverter really holds

    await entity.async_turn_on()

    assert polled._client.writes == [(20142, 0b0001_0011)]  # bits 0 and 1 kept


async def test_settings_switch_refuses_to_write_if_read_fails(polled):
    setting = switch_platform.Setting(4, "GridBuzz", False, True)
    entity = switch_platform.MustInverterSettingsSwitch(polled, setting)
    polled.data["SystemSetting"] = 0
    polled._client.fail_starts.add(20142)

    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()

    assert polled._client.writes == []
