from custom_components.must_inverter.helpers import enum_option, scale_value, to_int16


def test_scale_value_removes_float_noise():
    assert scale_value(287, 0.1) == 28.7
    assert scale_value(1234, 0.01) == 12.34
    assert scale_value(573, 0.1) == 57.3


def test_scale_value_passthrough():
    assert scale_value(5, 1) == 5
    assert scale_value(5, None) == 5
    assert scale_value("No errors", 1) == "No errors"


def test_enum_option_valid_and_invalid():
    options = ["", "a", "b"]
    assert enum_option(options, 1) == "a"
    assert enum_option(options, 0) is None  # unnamed slot
    assert enum_option(options, 3) is None  # out of range: no IndexError
    assert enum_option(options, -1) is None  # negative int16 must not wrap around
    assert enum_option(options, "1") is None


def test_to_int16():
    assert to_int16(0xFFFF) == -1
    assert to_int16(0x7FFF) == 32767
    assert to_int16(100) == 100
