# -*- coding: utf-8 -*-
"""
Modbus TCP 采集器（JetLinks 物模型接入）
================================================
职责：
  1. 作为 Modbus 主站读取从站保持寄存器，按 JetLinks 物模型格式上报；
  2. 订阅 JetLinks 下发 topic（read/write/function），收到指令后写寄存器并回复。

从站：地址见 config.MODBUS_HOST，寄存器区间 0x0000~0x0009，本设备 = 0x0007。

用法：
  python modbus_collector.py                        # 周期采集并上报（默认每次采集都上报）
  python modbus_collector.py --read                 # 读取一次并上报后退出
  python modbus_collector.py --write 0x0007 100     # 向本设备寄存器写入数值
  python modbus_collector.py --on-change            # 仅值变化时上报

上报 topic：/{productId}/{deviceId}/properties/report
下发 topic：/{productId}/{deviceId}/properties/read | write、/function/invoke

寄存器 -> 物模型属性标识：0x0007 -> register_0x0007（见 config.modbus_property_id）
"""
import argparse
import json
import logging
import signal
import threading
import time
from datetime import datetime

from pymodbus.client import ModbusTcpClient

import config
import jetlinks
import mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("modbus")

# 物模型「功能标识」，用于通过 JetLinks 功能按钮写寄存器
FUNCTION_WRITE_REGISTER = "write_register"


def _property_to_address(prop_id):
    """把物模型属性标识还原成寄存器地址，例如 register_0x0007 -> 0x0007。"""
    if isinstance(prop_id, str) and prop_id.startswith("register_0x"):
        return int(prop_id[len("register_0x"):], 16)
    return None


def _legacy_registers(props):
    """把物模型属性 {register_0x0007: v} 转成旧版寄存器 {0x0007: v}。"""
    out = {}
    for k, v in props.items():
        addr = _property_to_address(k)
        if addr is not None:
            out[f"0x{addr:04X}"] = v
    return out


def build_legacy_payload(collector, legacy_registers):
    """构造旧版自定义 payload（发到 iot/group8/.../data，供 MQTTX 直测）。"""
    collector.seq += 1
    ts = time.time()
    return {
        "type": "modbus",
        "device_id": collector.device_id,
        "group": collector.group,
        "slave": {"host": config.MODBUS_HOST, "port": config.MODBUS_PORT, "unit": config.MODBUS_UNIT},
        "registers": legacy_registers,
        "seq": collector.seq,
        "ts": round(ts, 3),
        "datetime": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


class ModbusCollector:
    """Modbus TCP 主站采集器，只操作本设备的寄存器区间。"""

    def __init__(self, device_id=None, group=None):
        self.device_id = device_id or config.MODBUS_DEVICE_ID
        self.group = group if group is not None else config.GROUP_ID
        self.start, self.end = config.group_register_range(self.group)
        self.count = self.end - self.start + 1
        self.seq = 0
        self._last = None
        self.client = ModbusTcpClient(
            config.MODBUS_HOST, port=config.MODBUS_PORT, timeout=config.MODBUS_TIMEOUT
        )

    # ---------- 连接 ----------
    def connect(self):
        if not self.client.connect():
            raise ConnectionError(
                f"Modbus 从站连接失败 {config.MODBUS_HOST}:{config.MODBUS_PORT}"
            )
        log.info("已连接 Modbus 从站 %s:%s (unit=%s)",
                 config.MODBUS_HOST, config.MODBUS_PORT, config.MODBUS_UNIT)

    def close(self):
        self.client.close()

    # ---------- 读 / 写 ----------
    def read_properties(self):
        """读取本设备寄存器，返回物模型属性 {属性标识: 数值}。"""
        rr = self.client.read_holding_registers(
            self.start, count=self.count, device_id=config.MODBUS_UNIT
        )
        if rr.isError():
            raise IOError(f"读取寄存器失败：{rr}")
        return {config.modbus_property_id(a): int(v)
                for a, v in zip(range(self.start, self.end + 1), rr.registers)}

    def write_register(self, address, value):
        """向单个寄存器写入数值，仅允许本设备区间。"""
        self._check_address(address)
        wr = self.client.write_register(address, int(value), device_id=config.MODBUS_UNIT)
        if wr.isError():
            raise IOError(f"写入寄存器失败：{wr}")
        log.info("已写入 0x%04X = %s", address, value)

    def write_registers(self, address, values):
        """向连续寄存器写入一组数值，仅允许本设备区间。"""
        for i in range(len(values)):
            self._check_address(address + i)
        wr = self.client.write_registers(address, [int(v) for v in values], device_id=config.MODBUS_UNIT)
        if wr.isError():
            raise IOError(f"写入寄存器失败：{wr}")
        log.info("已写入 0x%04X ~ 0x%04X", address, address + len(values) - 1)

    def _check_address(self, address):
        if not (self.start <= address <= self.end):
            raise ValueError(
                f"地址 0x{address:04X} 不在本设备区间 [0x{self.start:04X}-0x{self.end:04X}] 内，"
                f"请修改 GROUP_ID 或使用本设备的寄存器"
            )


# ---------- 下发指令处理 ----------
def _handle_read(collector, client, reply_topic, payload):
    message_id = payload.get("messageId")
    props = collector.read_properties()
    mqtt.publish_json(client, reply_topic, jetlinks.read_property_reply_payload(message_id, props),
                      qos=config.MQTT_QOS)
    log.info("收到读取指令，已回复：%s", props)


def _handle_write(collector, client, reply_topic, payload):
    message_id = payload.get("messageId")
    new_props = payload.get("properties", {}) or {}
    applied = {}
    for prop_id, value in new_props.items():
        addr = _property_to_address(prop_id)
        if addr is None:
            log.warning("无法识别的属性标识：%s，跳过", prop_id)
            continue
        try:
            collector.write_register(addr, value)
            applied[prop_id] = value
        except Exception as e:
            log.error("写寄存器 %s 失败：%s", prop_id, e)
    mqtt.publish_json(client, reply_topic, jetlinks.write_property_reply_payload(message_id, applied),
                      qos=config.MQTT_QOS)
    log.info("收到写指令，已写寄存器：%s", applied)


def _handle_function(collector, client, reply_topic, payload):
    message_id = payload.get("messageId")
    function_id = payload.get("function")
    inputs = payload.get("inputs", []) or []
    args = {item.get("name"): item.get("value") for item in inputs}

    if function_id == FUNCTION_WRITE_REGISTER:
        addr_raw = args.get("address", args.get("register"))
        value = args.get("value")
        # 兼容两种写法：0x0007 或 register_0x0007
        addr = _property_to_address(addr_raw) if isinstance(addr_raw, str) else None
        if addr is None and isinstance(addr_raw, str) and addr_raw.lower().startswith("0x"):
            addr = int(addr_raw, 16)
        if addr is None:
            output = f"地址参数缺失或格式错误：{addr_raw}"
            success = False
        else:
            try:
                collector.write_register(addr, value)
                output = {config.modbus_property_id(addr): int(value)}
                success = True
            except Exception as e:
                output = str(e)
                success = False
        log.info("收到功能调用 %s，结果 success=%s output=%s", function_id, success, output)
    else:
        output = f"未知功能：{function_id}"
        success = False
        log.warning("收到未知功能调用：%s", function_id)

    mqtt.publish_json(client, reply_topic, jetlinks.invoke_function_reply_payload(message_id, output, success),
                      qos=config.MQTT_QOS)
    # 功能调用后立即补发一次原始数据上报，EMQX 规则会转成属性上报，平台马上看到最新值
    if success:
        mqtt.publish_json(client, config.MODBUS_RAW_TOPIC, collector.read_properties(), qos=config.MQTT_QOS)


def _make_on_message(collector, client, product_id, device_id):
    """构造 on_message 回调，根据下发 topic 分发处理。"""
    read_topic = jetlinks.read_property_topic(product_id, device_id)
    write_topic = jetlinks.write_property_topic(product_id, device_id)
    invoke_topic = jetlinks.invoke_function_topic(product_id, device_id)
    read_reply = jetlinks.read_property_reply_topic(product_id, device_id)
    write_reply = jetlinks.write_property_reply_topic(product_id, device_id)
    invoke_reply = jetlinks.invoke_function_reply_topic(product_id, device_id)

    def on_message(c, userdata, msg):
        payload = jetlinks.parse_downlink(msg.payload)
        if payload is None:
            log.warning("无法解析下发消息：%s", msg.payload)
            return
        try:
            if msg.topic == read_topic:
                _handle_read(collector, client, read_reply, payload)
            elif msg.topic == write_topic:
                _handle_write(collector, client, write_reply, payload)
            elif msg.topic == invoke_topic:
                _handle_function(collector, client, invoke_reply, payload)
            else:
                log.warning("未知下发 topic：%s", msg.topic)
        except Exception as e:
            log.error("处理下发消息出错：%s", e)

    return on_message


# ---------- 主循环 ----------
def run_poll(on_change=False):
    """周期采集并上报。默认每次采集都上报；on_change=True 时仅在值变化时上报。"""
    collector = ModbusCollector()
    product_id = config.MODBUS_PRODUCT_ID
    device_id = collector.device_id

    raw_topic = config.MODBUS_RAW_TOPIC   # 原始寄存器数据发这里，由 EMQX 规则转成温湿度后转发到标准 topic
    read_topic = jetlinks.read_property_topic(product_id, device_id)
    write_topic = jetlinks.write_property_topic(product_id, device_id)
    invoke_topic = jetlinks.invoke_function_topic(product_id, device_id)
    online_topic = jetlinks.online_topic(product_id, device_id)
    offline_topic = jetlinks.offline_topic(product_id, device_id)

    legacy_data_topic = config.modbus_data_topic(device_id)
    legacy_status_topic = config.modbus_status_topic(device_id)

    client = mqtt.create_client(device_id, will_topic=legacy_status_topic)
    client.on_message = _make_on_message(collector, client, product_id, device_id)
    mqtt.connect(client, config.MQTT_BROKER, config.MQTT_PORT,
                 config.MQTT_KEEPALIVE, config.MQTT_USERNAME, config.MQTT_PASSWORD)

    # 订阅平台下发 topic
    client.subscribe(read_topic, qos=config.MQTT_QOS)
    client.subscribe(write_topic, qos=config.MQTT_QOS)
    client.subscribe(invoke_topic, qos=config.MQTT_QOS)

    # 上线通知（JetLinks + 旧版 status 双发）
    client.publish(online_topic, payload=json.dumps(jetlinks.online_payload()), qos=config.MQTT_QOS)
    mqtt.publish_status(client, legacy_status_topic, "online")

    stop_event = threading.Event()

    def _handle(signum, frame):
        log.info("收到退出信号，正在下线...")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    log.info("Modbus 采集器启动 device=%s group=%s 区间=[0x%04X-0x%04X]",
             collector.device_id, collector.group, collector.start, collector.end)
    log.info("原始数据主题：%s", raw_topic)
    log.info("订阅下发：%s / %s / %s", read_topic, write_topic, invoke_topic)

    try:
        collector.connect()
        while not stop_event.is_set():
            try:
                props = collector.read_properties()
                if not on_change or props != collector._last:
                    mqtt.publish_json(client, raw_topic, props, qos=config.MQTT_QOS)
                    mqtt.publish_json(client, legacy_data_topic,
                                      build_legacy_payload(collector, _legacy_registers(props)),
                                      qos=config.MQTT_QOS)
                    log.info("已上报寄存器（原始数据）：%s", props)
                collector._last = props
            except Exception as e:
                log.warning("采集失败：%s", e)
            stop_event.wait(config.MODBUS_POLL_INTERVAL)
    finally:
        client.publish(offline_topic, payload=json.dumps(jetlinks.offline_payload()), qos=config.MQTT_QOS)
        mqtt.publish_status(client, legacy_status_topic, "offline")
        client.loop_stop()
        client.disconnect()
        collector.close()
        log.info("已下线，退出")


def main():
    parser = argparse.ArgumentParser(description="Modbus TCP 数据采集器（JetLinks 物模型）")
    parser.add_argument("--read", action="store_true", help="只读取一次并上报后退出")
    parser.add_argument("--write", nargs=2, metavar=("ADDR", "VALUE"),
                        help="向本设备寄存器写入，例如 --write 0x0007 100")
    parser.add_argument("--on-change", action="store_true",
                        help="仅在寄存器值变化时上报（默认每次采集都上报）")
    args = parser.parse_args()

    if args.write:
        addr = int(args.write[0], 16)
        value = int(args.write[1])
        collector = ModbusCollector()
        try:
            collector.connect()
            collector.write_register(addr, value)
        finally:
            collector.close()
        return

    if args.read:
        collector = ModbusCollector()
        raw_topic = config.MODBUS_RAW_TOPIC
        legacy_data_topic = config.modbus_data_topic(collector.device_id)
        client = mqtt.create_client(collector.device_id)
        mqtt.connect(client, config.MQTT_BROKER, config.MQTT_PORT,
                     config.MQTT_KEEPALIVE, config.MQTT_USERNAME, config.MQTT_PASSWORD)
        try:
            collector.connect()
            props = collector.read_properties()
            mqtt.publish_json(client, raw_topic, props, qos=config.MQTT_QOS)
            mqtt.publish_json(client, legacy_data_topic,
                              build_legacy_payload(collector, _legacy_registers(props)),
                              qos=config.MQTT_QOS)
            log.info("已上报寄存器：%s", props)
        finally:
            client.loop_stop()
            client.disconnect()
            collector.close()
        return

    run_poll(args.on_change)


if __name__ == "__main__":
    main()
