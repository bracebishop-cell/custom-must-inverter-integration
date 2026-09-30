import datetime

from custom_components.must_inverter import mapper


def test_int16_sign():
    assert mapper.int16(1, {1: 0xFFFF}) == -1
    assert mapper.int16(1, {1: 5}) == 5


def test_serial_and_version():
    assert mapper.serial(10, {10: 1, 11: 2}) == (1 << 16) | 2
    assert mapper.version(0, {0: 10203}) == "1.2.3"


def test_time():
    assert mapper.time(0, {0: 1345}) == datetime.time(13, 45)


def test_accumulated_kwh():
    assert mapper.accumulated_kwh(0, {0: 2, 1: 35}) == 2 * 1000 + 35 * 0.1


def test_error_bits():
    codes = ["a", "b", "c"] + [""] * 13
    assert mapper.error_bits(0, {0: 0}, codes) == "No errors"
    assert mapper.error_bits(0, {0: 0b101}, codes) == "a, c"
    assert mapper.error_bits(0, {0: 1 << 5}, codes) == "Unknown error bit 5"
