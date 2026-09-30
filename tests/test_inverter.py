import asyncio

import pytest

from custom_components.must_inverter import STALE_AFTER_FAILURES
from custom_components.must_inverter.const import get_sensors_for_model
from tests.conftest import HomeAssistantError, make_inverter

# ---------------------------------------------------------------------------
# SAFETY: the exact (address, count) read requests sent to the inverter. These
# lists are copied from the original integration; they must never change by
# accident since some ranges are known to crash the inverter.
# ---------------------------------------------------------------------------
BASE_READS = [(10101, 24), (15201, 21), (20000, 17), (20101, 114), (25201, 79)]
PV1900_EXTRA_READS = [(113, 2), (15207, 2), (16205, 4)]
PH1100_READS = [(10102, 18), (15104, 16), (20001, 3), (25225, 115)]


@pytest.mark.asyncio
async def test_read_requests_unchanged_pv1800(inverter):
    assert await inverter.read_modbus_data() is True
    assert inverter._client.reads == BASE_READS
    assert inverter._client.writes == []


@pytest.mark.asyncio
async def test_read_requests_unchanged_pv1900(no_sleep):
    inverter = make_inverter("pv1900")
    assert await inverter.read_modbus_data() is True
    assert inverter._client.reads == BASE_READS + PV1900_EXTRA_READS


@pytest.mark.asyncio
async def test_read_requests_unchanged_ph1100(no_sleep):
    inverter = make_inverter("ph1100")
    await inverter.read_modbus_data()
    assert inverter._client.reads == PH1100_READS


@pytest.mark.asyncio
async def test_polling_never_writes(inverter):
    for _ in range(5):
        await inverter._async_refresh_modbus_data()
    assert inverter._client.writes == []


# --------------------------------------------------------------------- polling
@pytest.mark.asyncio
async def test_successful_poll(inverter):
    assert await inverter.read_modbus_data() is True
    assert inverter._reading is False
    assert inverter.last_successful_poll is not None
    assert inverter.key_available("InverterSerialNumber")


@pytest.mark.asyncio
async def test_reading_flag_reset_after_cancel(inverter):
    inverter._client.raise_on_start[15201] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await inverter.read_modbus_data()

    assert inverter._reading is False  # previously stuck on True forever

    inverter._client.raise_on_start.clear()
    assert await inverter.read_modbus_data() is True


@pytest.mark.asyncio
async def test_overlapping_read_is_skipped(inverter):
    inverter._reading = True
    assert await inverter.read_modbus_data() is None
    assert inverter._client.reads == []
    assert inverter._reading is True  # not touched by the skipped call


@pytest.mark.asyncio
async def test_failing_range_goes_stale_then_recovers(inverter):
    await inverter._async_refresh_modbus_data()
    battery_keys = inverter._range_keys[15201]
    assert "BatteryVoltage" in battery_keys

    inverter._client.fail_starts.add(15201)

    for _ in range(STALE_AFTER_FAILURES - 1):
        await inverter._async_refresh_modbus_data()
        assert inverter.key_available("BatteryVoltage")  # tolerate short glitches

    await inverter._async_refresh_modbus_data()
    assert not inverter.key_available("BatteryVoltage")
    assert inverter.key_available("InverterSerialNumber")  # other ranges unaffected

    inverter._client.fail_starts.clear()
    await inverter._async_refresh_modbus_data()
    assert inverter.key_available("BatteryVoltage")


@pytest.mark.asyncio
async def test_connection_loss_makes_everything_unavailable(inverter):
    await inverter._async_refresh_modbus_data()
    calls = []
    inverter.async_add_must_inverter_sensor(lambda: calls.append(1))

    inverter._client.connected = False
    inverter._client.can_connect = False

    for _ in range(STALE_AFTER_FAILURES):
        assert await inverter._async_refresh_modbus_data() is False

    assert len(calls) == STALE_AFTER_FAILURES  # entities are told about failures too
    assert not inverter.key_available("BatteryVoltage")
    assert not inverter.key_available("InverterSerialNumber")

    inverter._client.can_connect = True
    assert await inverter._async_refresh_modbus_data() is True
    assert inverter.key_available("BatteryVoltage")


@pytest.mark.asyncio
async def test_first_poll_without_serial_fails(inverter):
    inverter._client.fail_starts.add(20000)
    assert await inverter.read_modbus_data() is False


@pytest.mark.asyncio
async def test_all_reads_failing_returns_false(inverter):
    inverter._client.fail_all_reads = True
    assert await inverter.read_modbus_data() is False


# ---------------------------------------------------------------------- writes
@pytest.mark.asyncio
async def test_write_verified(inverter):
    result = await inverter.write_modbus_data(10103, 545)
    assert result == 545
    assert inverter._client.writes == [(10103, 545)]
    assert inverter._client.reads == [(10103, 1)]  # exactly one single-register read back


@pytest.mark.asyncio
async def test_write_readback_mismatch_returns_actual_value(inverter):
    inverter._client.write_readback = 540
    assert await inverter.write_modbus_data(10103, 545) == 540


@pytest.mark.asyncio
async def test_write_readback_failure_is_not_fatal(inverter):
    inverter._client.readback_error = True
    assert await inverter.write_modbus_data(10103, 545) == 545


@pytest.mark.asyncio
async def test_write_without_verify_does_not_read(inverter):
    assert await inverter.write_modbus_data(10112, 1, verify=False) == 1
    assert inverter._client.reads == []


@pytest.mark.asyncio
async def test_rejected_write_raises(inverter):
    inverter._client.reject_writes = True
    with pytest.raises(HomeAssistantError):
        await inverter.write_modbus_data(10103, 545)


@pytest.mark.asyncio
async def test_write_while_disconnected_raises_and_sends_nothing(inverter):
    inverter._client.connected = False
    inverter._client.can_connect = False
    with pytest.raises(HomeAssistantError):
        await inverter.write_modbus_data(10103, 545)
    assert inverter._client.writes == []


@pytest.mark.asyncio
async def test_read_single_register(inverter):
    inverter._client.values[20142] = 0b1010
    assert await inverter.read_single_register(20142) == 0b1010
    inverter._client.fail_starts.add(20142)
    assert await inverter.read_single_register(20142) is None


def test_every_model_has_sensor_list():
    for model in ("pv1800", "pv1900", "ph1100"):
        assert get_sensors_for_model(model)
