import asyncio
import logging
import contextlib

from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient, AsyncModbusUdpClient
from pymodbus.exceptions import ModbusException

from homeassistant.const import (
    CONF_NAME,
    CONF_DEVICE,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_MODE,
    CONF_HOST,
    CONF_PORT,
    CONF_TIMEOUT,
)

from .const import (
    DOMAIN,
    CONF_BAUDRATE,
    CONF_PARITY,
    CONF_STOPBITS,
    CONF_BYTESIZE,
    CONF_RETRIES,
    CONF_RECONNECT_DELAY,
    CONF_RECONNECT_DELAY_MAX,
    CONF_DEVICE_ID,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_DEVICE_ID,
    get_sensors_for_model,
    MODEL_PV1900,
    MODEL_PH1100,
)

from .helpers import to_int16
from .mapper import (
    convert_ph1100_partArr1,
    convert_ph1100_partArr2,
    convert_ph1100_partArr3,
    convert_ph1100_partArr4,
    convert_partArr2,
    convert_partArr3,
    convert_partArr4,
    convert_partArr5,
    convert_partArr6,
    convert_battery_status,
    convert_pv_data,
)
# from .utils.register_monitor import RegisterMonitor

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.TIME,
]
_LOGGER = logging.getLogger(__name__)

# A register range that fails this many polls in a row is considered stale and the
# entities fed by it become unavailable (instead of showing frozen values).
STALE_AFTER_FAILURES = 3
# Delay before reading a register back after writing it.
WRITE_VERIFY_DELAY = 0.3


async def async_setup(hass, config):
    hass.data[DOMAIN] = {}
    return True  # Return boolean to indicate that initialization was successful.


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the must inverter component."""
    try:
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))

        # Initialize inverter with model-specific sensors
        inverter = MustInverter(hass, entry)

        successConnecting = await inverter.connect()

        if not successConnecting:
            raise ConfigEntryNotReady("Unable to connect to modbus device")

        successReading = await inverter._async_refresh_modbus_data()

        if not successReading:
            raise ConfigEntryNotReady("Unable to read from modbus device")

        model = inverter.model
        sensors = get_sensors_for_model(model)
        _LOGGER.debug("Setting up Must Inverter with model: %s", model)

        # Store sensors to be used by platforms
        hass.data[DOMAIN][entry.entry_id] = {"inverter": inverter, "sensors": sensors}

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Removing register monitor as we've found all needed registers
        # If you need to add it back, uncomment the following lines and set the ranges to scan in register_monitor.py
        # monitor = RegisterMonitor(hass)

        # Add monitoring to the update cycle, but at a slower rate
        # async def delayed_monitor():
        #     """Run monitor at a slower rate to avoid overwhelming the device."""
        #     try:
        #         while True:
        #             await monitor.scan_ranges(inverter)
        #             await asyncio.sleep(300)  # Run every 5 minutes
        #     except asyncio.CancelledError:
        #         _LOGGER.debug("Register monitor task cancelled")
        #         raise

        # monitor_task = asyncio.create_task(delayed_monitor())

        # Store the task so we can cancel it later
        # hass.data[DOMAIN][entry.entry_id]["monitor_task"] = monitor_task

        return True
    except ConfigEntryNotReady as ex:
        raise ex
    except Exception as ex:
        _LOGGER.exception("Error setting up modbus device", exc_info=True)
        raise ConfigEntryNotReady("Unknown error connecting to modbus device") from ex


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Cancel the monitor task
        if monitor_task := hass.data[DOMAIN][entry.entry_id].get("monitor_task"):
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task

        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener, called when the config entry options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


class MustInverter:
    def __init__(self, hass, entry: ConfigEntry):
        self._hass = hass
        self._callbacks = []  # Store callbacks separately
        self._entry = entry
        # Check both data and options

        common = {
            "timeout": entry.options[CONF_TIMEOUT],
            "retries": entry.options[CONF_RETRIES],
            "reconnect_delay": entry.options[CONF_RECONNECT_DELAY],
            "reconnect_delay_max": entry.options[CONF_RECONNECT_DELAY_MAX],
        }

        match entry.options[CONF_MODE]:
            case "serial":
                self._client = AsyncModbusSerialClient(
                    entry.options[CONF_DEVICE],
                    baudrate=entry.options[CONF_BAUDRATE],
                    stopbits=entry.options[CONF_STOPBITS],
                    bytesize=entry.options[CONF_BYTESIZE],
                    parity=entry.options[CONF_PARITY],
                    **common,
                )
            case "tcp":
                self._client = AsyncModbusTcpClient(entry.options[CONF_HOST], port=entry.options[CONF_PORT], **common)
            case "udp":
                self._client = AsyncModbusUdpClient(entry.options[CONF_HOST], port=entry.options[CONF_PORT], **common)
            case _:
                raise Exception("Invalid mode")

        self._client.rts = False
        self._client.dtr = False
        self._lock = asyncio.Lock()
        self._device_id = entry.options.get(CONF_DEVICE_ID, DEFAULT_DEVICE_ID)
        self._scan_interval = timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        self._reading = False
        self.registers = {}
        self.data = {}
        # Data freshness tracking: which keys each register range produces, how many
        # polls in a row each range failed, and which keys are currently stale.
        self._range_keys: dict[int, set[str]] = {}
        self._range_failures: dict[int, int] = {}
        self._stale_keys: set[str] = set()
        self.last_successful_poll: datetime | None = None

    @callback
    def async_add_must_inverter_sensor(self, update_callback):
        # This is the first sensor, set up interval.
        if not self._callbacks:
            self._unsub_interval_method = async_track_time_interval(
                self._hass, self._async_refresh_modbus_data, self._scan_interval
            )

        self._callbacks.append(update_callback)

    @callback
    def async_remove_must_inverter_sensor(self, update_callback):
        self._callbacks.remove(update_callback)

        if not self._callbacks:
            """stop the interval timer upon removal of last sensor"""
            self._unsub_interval_method()
            self._unsub_interval_method = None
            self.close()

    def key_available(self, key: str) -> bool:
        """Return True if key has a value that is still being refreshed."""
        return key in self.data and key not in self._stale_keys

    @property
    def stale_keys(self) -> set[str]:
        return set(self._stale_keys)

    def _update_range_health(self, results: dict[int, bool]) -> None:
        """Record which ranges were read successfully in the last poll."""
        for start, ok in results.items():
            keys = self._range_keys.get(start, set())
            if ok:
                self._range_failures[start] = 0
                self._stale_keys -= keys
                continue

            failures = self._range_failures.get(start, 0) + 1
            self._range_failures[start] = failures

            if failures >= STALE_AFTER_FAILURES and keys - self._stale_keys:
                _LOGGER.warning(
                    "register range starting at %s failed %s polls in a row, marking its entities unavailable",
                    start,
                    failures,
                )
                self._stale_keys |= keys

    def _notify(self) -> None:
        for update_callback in list(self._callbacks):
            update_callback()

    async def _async_refresh_modbus_data(self, now=None):
        if not await self._check_and_reopen():
            # if not connected, skip
            _LOGGER.warning("not connected, skipping refresh")
            self._update_range_health(dict.fromkeys(self._range_keys, False))
            self._notify()
            return False

        try:
            update_result = await self.read_modbus_data()
        except Exception:
            _LOGGER.exception("error reading inverter data", exc_info=True)
            self._update_range_health(dict.fromkeys(self._range_keys, False))
            update_result = False

        if update_result is None:
            # previous read still running, nothing changed
            return False

        # Always notify, also on failure, so entities can update their availability
        self._notify()
        return update_result

    @property
    def name(self):
        return self._entry.options.get(CONF_NAME, self.data.get("InverterMachineType", self.model))

    @property
    def model(self):
        from_config = self._entry.options.get(CONF_MODEL)
        detected = self.data.get("InverterMachineType")

        if from_config == "autodetect":
            return detected

        return from_config or detected

    @property
    def has_extra_registers(self):
        return self.model == MODEL_PV1900

    def close(self):
        _LOGGER.info("closing modbus client")
        self._client.close()

    async def _check_and_reopen(self):
        if not self._client.connected:
            _LOGGER.info("modbus client is not connected, trying to reconnect")
            return await self.connect()

        return self._client.connected

    async def connect(self):
        result = False

        _LOGGER.debug("connecting to %s:%s", self._client.comm_params.host, self._client.comm_params.port)

        async with self._lock:
            result = await self._client.connect()

        if result:
            _LOGGER.info(
                "successfully connected to %s:%s", self._client.comm_params.host, self._client.comm_params.port
            )
        else:
            _LOGGER.warning(
                "not able to connect to %s:%s", self._client.comm_params.host, self._client.comm_params.port
            )
        return result

    async def write_modbus_data(self, address, value, verify=True):
        """Write a single holding register.

        Raises HomeAssistantError if the write could not be performed or was
        rejected by the inverter, so callers never report a failed write as done.

        With verify=True the register is read back once. The returned value is
        the value the register should be considered to hold: the written value if
        the read back matches (or could not be done), otherwise the value read
        back. Use verify=False for momentary/command registers.
        """
        if not await self._check_and_reopen():
            raise HomeAssistantError(f"Unable to write register {address}: not connected to the inverter")

        _LOGGER.debug("writing modbus data: %s %s", address, value)

        try:
            async with self._lock:
                response = await self._client.write_register(address=address, value=value, device_id=self._device_id)
        except (ModbusException, OSError) as err:
            raise HomeAssistantError(f"Error writing register {address}: {err}") from err

        if response.isError():
            _LOGGER.error("error writing modbus data: %s", response)
            raise HomeAssistantError(f"The inverter rejected the write to register {address}: {response}")

        _LOGGER.debug("successfully wrote modbus data: %s %s", address, value)

        if not verify:
            return value

        await asyncio.sleep(WRITE_VERIFY_DELAY)

        try:
            async with self._lock:
                check = await self._client.read_holding_registers(address=address, count=1, device_id=self._device_id)
        except (ModbusException, OSError) as err:
            _LOGGER.debug("could not read back register %s: %s", address, err)
            return value

        if check.isError() or len(check.registers) != 1:
            _LOGGER.debug("could not read back register %s: %s", address, check)
            return value

        actual = check.registers[0]

        if actual != value & 0xFFFF:
            _LOGGER.warning(
                "register %s reads back %s after writing %s, the inverter may not have accepted the value",
                address,
                actual,
                value,
            )
            return to_int16(actual)

        return value

    async def read_single_register(self, address):
        """Read one holding register, returns None if it could not be read."""
        if not await self._check_and_reopen():
            return None

        try:
            async with self._lock:
                response = await self._client.read_holding_registers(
                    address=address, count=1, device_id=self._device_id
                )
        except (ModbusException, OSError) as err:
            _LOGGER.debug("could not read register %s: %s", address, err)
            return None

        if response.isError() or len(response.registers) != 1:
            return None

        return response.registers[0]

    async def read_modbus_data(self):
        """Read all register ranges.

        Returns True if at least one range was read, False if nothing could be
        read, and None if the read was skipped because another one is running.
        """
        _LOGGER.debug("reading modbus data")

        if self._reading:
            _LOGGER.warning(
                "skipping reading modbus data, previous read still in progress, make sure your scan interval is not too low"
            )
            return None

        self._reading = True

        try:
            return await self._read_all_ranges()
        finally:
            # also runs when the read is cancelled or raises, otherwise every
            # following poll would be skipped forever
            self._reading = False

    async def _read_all_ranges(self):
        # Reading all the control messages at once (ranges 20016-20100 and/or
        # 10009-10100 are problematic) causes the inverter to shut down
        # immediately and the charger stops working afterwards (all registers
        # return zeroes). Requires the grid/batteries/PV to be disconnected and
        # the inverter restarted for it to come back online.
        if self.model == MODEL_PH1100:
            registersAddresses = [
                (10102, 10119, convert_ph1100_partArr1),  # Charger Control Messages
                (15104, 15119, convert_ph1100_partArr2),  # Charger Display Messages
                (20001, 20003, convert_ph1100_partArr3),  # Inverter Control Messages
                (25225, 25339, convert_ph1100_partArr4),  # Inverter Display Messages
            ]
        else:
            # Base register ranges for all models
            registersAddresses = [
                # (10000, 10008, convert_partArr1), # Charger Control Messages
                (10101, 10124, convert_partArr2),  # Charger Control Messages
                (15201, 15221, convert_partArr3),  # Charger Display Messages
                (20000, 20016, convert_partArr4),  # Inverter Control Messages
                (20101, 20214, convert_partArr5),  # Inverter Control Messages
                (25201, 25279, convert_partArr6),  # Inverter Display Messages
            ]

            # Add PV19-specific register ranges if needed
            # Keep register ranges for pv separate to not overload the inverter
            if self.has_extra_registers:
                registersAddresses.extend(
                    [
                        (113, 114, convert_battery_status),  # Battery Status (SoC, SoH)
                        (15207, 15208, convert_pv_data),  # PV1 Data (Current, Power)
                        (16205, 16208, convert_pv_data),  # PV2 Data (Voltage, Current, Power)
                    ]
                )

        read = {}
        results = {register[0]: False for register in registersAddresses}

        for register in registersAddresses:
            if self.has_extra_registers:
                # Add a small delay between reading standard and PV19-specific registers
                # to prevent any potential issues
                await asyncio.sleep(0.1)

            start = register[0]
            end = register[1]
            count = end - start + 1

            try:
                _LOGGER.debug("reading modbus data from %s to %s (%s)", start, end, count)

                if not await self._check_and_reopen():
                    break

                async with self._lock:
                    response = await self._client.read_holding_registers(
                        address=start, count=count, device_id=self._device_id
                    )
                if response.isError():
                    _LOGGER.error("error reading modbus data at address %s: %s", start, response)
                elif len(response.registers) != count:
                    _LOGGER.warning(
                        "wrong number of registers read at address %s, expected %s, got %s - this usually is caused by concurrent access to modbus (usb/wifi/rs485), please remove other devices",
                        start,
                        count,
                        len(response.registers),
                    )
                    # Log the actual values received
                    _LOGGER.debug("Received registers (wrong): %s", response.registers)

                    ir.async_create_issue(
                        self._hass,
                        DOMAIN,
                        "mismatched_registers",
                        is_fixable=False,
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="mismatched_registers",
                        is_persistent=False,
                        learn_more_url="https://github.com/mukaschultze/ha-must-inverter/issues/47",
                    )
                else:
                    _LOGGER.debug("Correctly read %s registers from start %s", count, start)
                    for i in range(count):
                        read[start + i] = response.registers[i]
                    converted = register[2](read) or {}
                    self.data.update(converted)
                    self._range_keys[start] = set(converted)
                    results[start] = True
            except asyncio.exceptions.CancelledError:
                _LOGGER.warning("cancelled modbus read")
                raise
            except Exception:
                _LOGGER.error("error reading modbus data at address %s", start, exc_info=True)

        _LOGGER.debug("finished reading modbus data, %s", read)
        self.registers = read
        self._update_range_health(results)

        if not any(results.values()):
            return False

        serial_number = self.data.get("InverterSerialNumber")

        if serial_number is None:
            # can't identify the device (serial range never read successfully)
            return False

        self.last_successful_poll = datetime.now(UTC)

        if serial_number == 0xFFFFFFFF or serial_number == 0:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                "no_serial_number",
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="no_serial_number",
                data={"entry_id": self._entry.entry_id},
            )

        return True

    def _device_info(self):
        return {
            "identifiers": {(DOMAIN, self.data["InverterSerialNumber"])},
            "name": self.name,
            "manufacturer": "Must Solar",
            "model": self.data.get("InverterMachineType", self.model),
            "hw_version": self.data.get("InverterHardwareVersion"),
            "sw_version": self.data.get("InverterSoftwareVersion"),
            "serial_number": self.data["InverterSerialNumber"],
        }
