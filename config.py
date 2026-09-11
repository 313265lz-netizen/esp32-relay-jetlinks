# -*- coding: utf-8 -*-
"""全局配置，三个脚本共用，可通过环境变量覆盖。"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env_int(name, default):
    return int(os.getenv(name, str(default)))


def _env_float(name, default):
    return float(os.getenv(name, str(default)))


def _env_str(name, default):
    return os.getenv(name, default)


# 设备分组（决定占用的 Modbus 寄存器段）
GROUP_ID = _env_int("GROUP_ID", 8)

# MQTT 服务器
MQTT_BROKER = _env_str("MQTT_BROKER", "")
MQTT_PORT = _env_int("MQTT_PORT", 9783)
MQTT_KEEPALIVE = _env_int("MQTT_KEEPALIVE", 60)
MQTT_QOS = _env_int("MQTT_QOS", 1)

# JetLinks 物模型接入
SENSOR_PRODUCT_ID = _env_str("SENSOR_PRODUCT_ID", "sss1")
SENSOR_DEVICE_ID = _env_str("SENSOR_DEVICE_ID", "sss1")

MODBUS_PRODUCT_ID = _env_str("MODBUS_PRODUCT_ID", "sss1")
MODBUS_DEVICE_ID = _env_str("MODBUS_DEVICE_ID", "modbus-01")

MQTT_USERNAME = _env_str("MQTT_USERNAME", "")
MQTT_PASSWORD = _env_str("MQTT_PASSWORD", "")

SENSOR_PROP_TEMP = _env_str("SENSOR_PROP_TEMP", "temperature")
SENSOR_PROP_HUM = _env_str("SENSOR_PROP_HUM", "humidity")

# 设备 1：温湿度模拟器
SENSOR_JSON_FILE = _env_str("SENSOR_JSON_FILE", "sensor.json")
SENSOR_INTERVAL = _env_float("SENSOR_INTERVAL", 2.0)
SENSOR_HEARTBEAT = _env_float("SENSOR_HEARTBEAT", 30.0)

# 设备 2：Modbus TCP
MODBUS_HOST = _env_str("MODBUS_HOST", "127.0.0.1")
MODBUS_PORT = _env_int("MODBUS_PORT", 5502)
MODBUS_UNIT = _env_int("MODBUS_UNIT", 1)
MODBUS_TIMEOUT = _env_float("MODBUS_TIMEOUT", 3.0)
MODBUS_POLL_INTERVAL = _env_float("MODBUS_POLL_INTERVAL", 5.0)
MODBUS_RAW_TOPIC = _env_str("MODBUS_RAW_TOPIC", "/modbus/raw/modbus-01")

# 设备 3：8 路继电器
RELAY_PRODUCT_ID = _env_str("RELAY_PRODUCT_ID", "sss1")
RELAY_DEVICE_ID = _env_str("RELAY_DEVICE_ID", "relay-g8")
RELAY_CHANNELS = _env_int("RELAY_CHANNELS", 8)
RELAY_PROP_PREFIX = _env_str("RELAY_PROP_PREFIX", "switch")
RELAY_POLL_INTERVAL = _env_float("RELAY_POLL_INTERVAL", 2.0)
RELAY_HEARTBEAT = _env_float("RELAY_HEARTBEAT", 30.0)

# 寄存器分配
REGISTER_START = _env_int("REGISTER_START", 0x0000)
REGISTER_TOTAL = 10
REGISTERS_PER_GROUP = 1


def group_register_range(group_id=None):
    gid = group_id if group_id is not None else GROUP_ID
    start = REGISTER_START + (gid - 1) * REGISTERS_PER_GROUP
    end = start + REGISTERS_PER_GROUP - 1
    if start < REGISTER_START or end >= REGISTER_START + REGISTER_TOTAL:
        raise ValueError(
            f"GROUP_ID={gid} 超出寄存器范围：最多 {REGISTER_TOTAL // REGISTERS_PER_GROUP} 组"
        )
    return start, end


def modbus_property_id(register_addr):
    return f"register_0x{register_addr:04X}"


def relay_property_id(channel):
    return f"{RELAY_PROP_PREFIX}_{channel}"


# 旧版自定义主题
def _topic(kind, device_id, suffix):
    return f"iot/group{GROUP_ID}/{kind}/{device_id}/{suffix}"


def sensor_data_topic(device_id=None):
    return _topic("sensor", device_id or SENSOR_DEVICE_ID, "data")


def sensor_status_topic(device_id=None):
    return _topic("sensor", device_id or SENSOR_DEVICE_ID, "status")


def modbus_data_topic(device_id=None):
    return _topic("modbus", device_id or MODBUS_DEVICE_ID, "data")


def modbus_status_topic(device_id=None):
    return _topic("modbus", device_id or MODBUS_DEVICE_ID, "status")
