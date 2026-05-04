from app.services.xray_device_parser import (
    build_device_hash,
    parse_keyed_email,
    parse_log_line,
)


def test_parse_keyed_email_valid() -> None:
    assert parse_keyed_email("user_123_45") == (123, 45)
    assert parse_keyed_email("user_123_45_ws") == (123, 45)


def test_parse_keyed_email_invalid() -> None:
    assert parse_keyed_email("123-reality") is None
    assert parse_keyed_email("user_abc_45") is None
    assert parse_keyed_email("") is None


def test_device_hash_ignores_ip_by_design() -> None:
    assert build_device_hash("v2raytun/android") == build_device_hash("v2raytun/android")


def test_parse_log_line_plain_text() -> None:
    line = 'accepted user_2091126912_2 from 1.2.3.4 ua=v2raytun/android'
    parsed = parse_log_line(line)
    assert parsed is not None
    assert parsed["user_id"] == 2091126912
    assert parsed["key_id"] == 2
    assert parsed["ip"] == "1.2.3.4"
    assert "v2raytun/android" in parsed["user_agent"]


def test_parse_log_line_invalid_email_filtered() -> None:
    line = 'accepted 2091126912-reality from 1.2.3.4 ua=v2raytun/android'
    assert parse_log_line(line) is None
