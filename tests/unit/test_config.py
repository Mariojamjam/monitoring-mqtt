import pytest

from monitoring_mqtt.core.config import (
    ConfigError,
    MqttSettings,
    env_bool,
    env_float,
    log_level_from_env,
)


def test_defaults() -> None:
    assert MqttSettings.from_env({}) == MqttSettings("localhost", 1883, 1, 30, True, 1, 30)


def test_reads_environment() -> None:
    env = {"MQTT_HOST": "broker", "MQTT_PORT": "1884", "MQTT_QOS": "0", "MQTT_KEEPALIVE": "60"}
    assert MqttSettings.from_env(env) == MqttSettings("broker", 1884, 0, 60)


def test_session_and_reconnect_from_environment() -> None:
    env = {
        "MQTT_CLEAN_SESSION": "false",
        "MQTT_RECONNECT_MIN_DELAY_SECONDS": "2",
        "MQTT_RECONNECT_MAX_DELAY_SECONDS": "8",
    }
    settings = MqttSettings.from_env(env)
    assert (settings.clean_session, settings.reconnect_min_delay, settings.reconnect_max_delay) == (
        False,
        2,
        8,
    )
    with pytest.raises(ConfigError, match="MAX_DELAY"):
        MqttSettings.from_env({**env, "MQTT_RECONNECT_MAX_DELAY_SECONDS": "1"})


@pytest.mark.parametrize(
    ("raw", "value"), [("true", True), ("ON", True), ("0", False), ("no", False)]
)
def test_env_bool(raw: str, value: bool) -> None:
    assert env_bool({"X": raw}, "X", not value) is value
    assert env_bool({}, "X", value) is value
    with pytest.raises(ConfigError):
        env_bool({"X": "maybe"}, "X", True)


@pytest.mark.parametrize("env", [{"MQTT_PORT": "abc"}, {"MQTT_PORT": "70000"}, {"MQTT_QOS": "3"}])
def test_invalid_values(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        MqttSettings.from_env(env)


def test_env_float_rejects_nan_and_below_minimum() -> None:
    with pytest.raises(ConfigError):
        env_float({"X": "nan"}, "X", 1.0, minimum=0.1)
    with pytest.raises(ConfigError):
        env_float({"X": "0"}, "X", 1.0, minimum=0.1)
    assert env_float({}, "X", 5.0, minimum=0.1) == 5.0


def test_log_level() -> None:
    assert log_level_from_env({"LOG_LEVEL": "debug"}) == "DEBUG"
    with pytest.raises(ConfigError):
        log_level_from_env({"LOG_LEVEL": "verbose"})
