"""Test setup: stub the parts of Home Assistant the integration imports at module level.

Only pymodbus is real; the inverter is replaced with a fake client, so no hardware
and no Home Assistant installation is needed to run these tests.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _HAError(Exception):
    pass


class ConfigEntryNotReady(_HAError):
    pass


class HomeAssistantError(_HAError):
    pass


class ServiceValidationError(HomeAssistantError):
    pass


def _install_stubs():
    if "homeassistant" in sys.modules and not isinstance(sys.modules["homeassistant"], types.ModuleType):
        return

    _module("homeassistant")
    _module("homeassistant.config_entries", ConfigEntry=object)
    _module(
        "homeassistant.const",
        Platform=MagicMock(),
        UnitOfEnergy=MagicMock(),
        CONF_NAME="name",
        CONF_DEVICE="device",
        CONF_MODEL="model",
        CONF_SCAN_INTERVAL="scan_interval",
        CONF_MODE="mode",
        CONF_HOST="host",
        CONF_PORT="port",
        CONF_TIMEOUT="timeout",
    )
    _module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _module(
        "homeassistant.exceptions",
        ConfigEntryNotReady=ConfigEntryNotReady,
        HomeAssistantError=HomeAssistantError,
        ServiceValidationError=ServiceValidationError,
    )
    _module("homeassistant.helpers")
    issue_registry = _module("homeassistant.helpers.issue_registry", async_create_issue=MagicMock(), IssueSeverity=MagicMock())
    sys.modules["homeassistant.helpers"].issue_registry = issue_registry
    _module("homeassistant.helpers.event", async_track_time_interval=MagicMock(return_value=MagicMock()))
    _module("homeassistant.components")
    for name in ("binary_sensor", "number", "sensor", "switch", "select", "time", "button"):
        _component(f"homeassistant.components.{name}")


class _Entity:
    """Stand-in for Home Assistant entity base classes."""

    def async_write_ha_state(self):
        pass


def _component(name):
    module = types.ModuleType(name)

    def __getattr__(attr):
        if attr.endswith("Entity"):
            return type(attr, (_Entity,), {})
        return MagicMock()

    module.__getattr__ = __getattr__
    sys.modules[name] = module


_install_stubs()


class FakeResponse:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self._error = error

    def isError(self):
        return self._error


class FakeClient:
    """Minimal stand-in for the pymodbus client, recording every request."""

    def __init__(self):
        self.connected = True
        self.can_connect = True
        self.comm_params = types.SimpleNamespace(host="127.0.0.1", port=502)
        self.values = {20002: 1, 20003: 2}  # non-zero serial number
        self.fail_starts = set()  # read requests starting here return an error
        self.fail_all_reads = False
        self.reject_writes = False
        self.write_readback = None  # None: echo what was written; else fixed value
        self.readback_error = False
        self.raise_on_start = {}  # start -> exception to raise
        self.reads = []  # (address, count)
        self.writes = []  # (address, value)

    async def connect(self):
        self.connected = self.can_connect
        return self.can_connect

    def close(self):
        self.connected = False

    async def read_holding_registers(self, address, count, device_id):
        self.reads.append((address, count))
        if address in self.raise_on_start:
            raise self.raise_on_start[address]
        if self.fail_all_reads or address in self.fail_starts:
            return FakeResponse(error=True)
        if count == 1 and self.writes and address == self.writes[-1][0]:
            if self.readback_error:
                return FakeResponse(error=True)
            if self.write_readback is not None:
                return FakeResponse([self.write_readback])
        return FakeResponse([self.values.get(address + i, 0) for i in range(count)])

    async def write_register(self, address, value, device_id):
        self.writes.append((address, value))
        if self.reject_writes:
            return FakeResponse(error=True)
        self.values[address] = value
        return FakeResponse([value])


def make_inverter(model="pv1800"):
    from custom_components.must_inverter import MustInverter

    entry = types.SimpleNamespace(
        entry_id="test",
        options={
            "mode": "tcp",
            "host": "127.0.0.1",
            "port": 502,
            "timeout": 1.0,
            "retries": 1,
            "reconnect_delay": 0.1,
            "reconnect_delay_max": 0.2,
            "device_id": 4,
            "model": model,
            "scan_interval": 15,
        },
    )
    inverter = MustInverter(MagicMock(), entry)
    inverter._client = FakeClient()
    return inverter


@pytest.fixture
def no_sleep(monkeypatch):
    import asyncio

    async def _instant(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


@pytest_asyncio.fixture
async def inverter(no_sleep):
    return make_inverter("pv1800")
