# -*- coding: utf-8 -*-
"""
8 路继电器模拟器（JetLinks 物模型接入）
================================================
职责：
  1. 读取 Modbus 寄存器（0x0007），用其低 8 位（bit0~bit7）各标识一路开关：
     0 关 / 1 开，共 8 路；
  2. 按 JetLinks 物模型格式把 8 路开关状态上报到 /{productId}/{deviceId}/properties/report；
  3. 订阅平台下发 topic（read/write）：平台写某一路开关 -> 翻转对应 bit 写回寄存器 -> 回读并上报。

从站：地址见 config.MODBUS_HOST，寄存器区间 0x0000~0x0009，本设备 = 0x0007。

用法：
  python relay_simulator.py                 # 周期读取寄存器，变化或心跳时上报 8 路开关状态
  python relay_simulator.py --read          # 读取一次 8 路状态并上报后退出
  python relay_simulator.py --set 3 1       # 开第 3 路（1=开）后退出
  python relay_simulator.py --set 3 0       # 关第 3 路（0=关）后退出

上报 topic：/{productId}/{deviceId}/properties/report
下发 topic：/{productId}/{deviceId}/properties/read | write
属性标识：{prefix}_1 ~ {prefix}_8（默认 switch_1 ~ switch_8，需与 JetLinks 物模型一致）
"""
import argparse
import json
import logging
import signal
import threading
import time

from pymodbus.client import ModbusTcpClient

import config
import jetlinks
import mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("relay")

# 8 路开关占用寄存器的低 8 位
CHANNEL_MASK = 0xFF


def _to_switch(value):
    """把任意输入归一化为开关值：0 表示关，非 0 表示开（1）。"""
    try:
        return 1 if int(value) != 0 else 0
    except (TypeError, ValueError):
        return 0


def _property_to_channel(prop_id):
    """把物模型属性标识 switch_3 还原成路号 3。"""
    prefix = config.RELAY_PROP_PREFIX + "_"
    if isinstance(prop_id, str) and prop_id.startswith(prefix):
        try:
            return int(prop_id[len(prefix):])
        except ValueError:
            return None
    return None


class RelayDevice:
    """8 路继电器模拟设备：用寄存器低 8 位各管一路开关。"""

    def __init__(self, device_id=None, group=None):
        self.device_id = device_id or config.RELAY_DEVICE_ID
        self.group = group if group is not None else config.GROUP_ID
        self.start, self.end = config.group_register_range(self.group)
        self.address = self.start          # 寄存器地址（0x0007）
        self.channels = config.RELAY_CHANNELS
        self._lock = threading.Lock()
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
    def read_register(self):
        """读取寄存器，返回低 8 位整数值（0~255）。"""
        rr = self.client.read_holding_registers(
            self.address, count=1, device_id=config.MODBUS_UNIT
        )
        if rr.isError():
            raise IOError(f"读取寄存器失败：{rr}")
        return int(rr.registers[0]) & CHANNEL_MASK

    def read_channels(self):
        """读取 8 路开关状态，返回 {属性标识: 0/1}。"""
        return self._props_from_value(self.read_register())

    def write_channel(self, channel, state):
        """写某一路开关（channel 1~8，state 0/1），保留其余位。"""
        if not (1 <= channel <= self.channels):
            raise ValueError(f"路号 {channel} 超出范围 1~{self.channels}")
        bit = 1 << (channel - 1)
        with self._lock:
            current = self.read_register()
            new_value = (current | bit) if state else (current & ~bit)
            new_value &= CHANNEL_MASK
            wr = self.client.write_register(
                self.address, new_value, device_id=config.MODBUS_UNIT
            )
        if wr.isError():
            raise IOError(f"写入寄存器失败：{wr}")
        log.info("已写继电器第 %s 路 = %s（寄存器 0x%04X = %s）",
                 channel, state, self.address, new_value)
        return new_value

    def _props_from_value(self, value):
        return {config.relay_property_id(ch): (value >> (ch - 1)) & 1
                for ch in range(1, self.channels + 1)}


# ---------- 下发指令处理 ----------
def _handle_read(relay, client, reply_topic, payload):
    message_id = payload.get("messageId")
    props = relay.read_channels()
    mqtt.publish_json(client, reply_topic, jetlinks.read_property_reply_payload(message_id, props),
                      qos=config.MQTT_QOS)
    log.info("收到读取指令，已回复：%s", props)


def _handle_write(relay, client, reply_topic, report_topic, payload):
    message_id = payload.get("messageId")
    new_props = payload.get("properties", {}) or {}
    applied = {}
    for prop_id, value in new_props.items():
        ch = _property_to_channel(prop_id)
        if ch is None:
            log.warning("无法识别的属性标识：%s，跳过", prop_id)
            continue
        try:
            relay.write_channel(ch, _to_switch(value))
            applied[prop_id] = _to_switch(value)
        except Exception as e:
            log.error("写继电器第 %s 路失败：%s", ch, e)

    # 回复平台
    mqtt.publish_json(client, reply_topic, jetlinks.write_property_reply_payload(message_id, applied),
                      qos=config.MQTT_QOS)
    # 立即把当前真实状态上报一次，让平台界面同步
    if applied:
        mqtt.publish_json(client, report_topic,
                          jetlinks.report_property_payload(relay.read_channels()),
                          qos=config.MQTT_QOS)
    log.info("收到写指令，已执行：%s", applied)


def _make_on_message(relay, client, product_id, device_id):
    """构造 on_message 回调，根据下发 topic 分发处理。"""
    report_topic = jetlinks.report_property_topic(product_id, device_id)
    read_topic = jetlinks.read_property_topic(product_id, device_id)
    write_topic = jetlinks.write_property_topic(product_id, device_id)
    read_reply = jetlinks.read_property_reply_topic(product_id, device_id)
    write_reply = jetlinks.write_property_reply_topic(product_id, device_id)

    def on_message(c, userdata, msg):
        payload = jetlinks.parse_downlink(msg.payload)
        if payload is None:
            log.warning("无法解析下发消息：%s", msg.payload)
            return
        try:
            if msg.topic == read_topic:
                _handle_read(relay, client, read_reply, payload)
            elif msg.topic == write_topic:
                _handle_write(relay, client, write_reply, report_topic, payload)
            else:
                log.warning("未知下发 topic：%s", msg.topic)
        except Exception as e:
            log.error("处理下发消息出错：%s", e)

    return on_message


# ---------- 主循环 ----------
def run_poll():
    relay = RelayDevice()
    product_id = config.RELAY_PRODUCT_ID
    device_id = relay.device_id

    report_topic = jetlinks.report_property_topic(product_id, device_id)
    read_topic = jetlinks.read_property_topic(product_id, device_id)
    write_topic = jetlinks.write_property_topic(product_id, device_id)
    online_topic = jetlinks.online_topic(product_id, device_id)
    offline_topic = jetlinks.offline_topic(product_id, device_id)

    client = mqtt.create_client(device_id)
    client.on_message = _make_on_message(relay, client, product_id, device_id)
    mqtt.connect(client, config.MQTT_BROKER, config.MQTT_PORT,
                 config.MQTT_KEEPALIVE, config.MQTT_USERNAME, config.MQTT_PASSWORD)

    # 订阅平台下发 topic
    client.subscribe(read_topic, qos=config.MQTT_QOS)
    client.subscribe(write_topic, qos=config.MQTT_QOS)

    # 上线通知
    client.publish(online_topic, payload=json.dumps(jetlinks.online_payload()), qos=config.MQTT_QOS)

    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        log.info("收到退出信号，正在下线...")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("8 路继电器模拟器启动 device=%s group=%s 寄存器=0x%04X 路数=%s",
             relay.device_id, relay.group, relay.address, relay.channels)
    log.info("上报主题：%s", report_topic)
    log.info("订阅下发：%s / %s", read_topic, write_topic)

    last = None
    last_heartbeat = 0.0
    try:
        relay.connect()
        while not stop_event.is_set():
            try:
                value = relay.read_register()
                now = time.time()
                # 变化时上报；同时按心跳周期定时补报一次作为心跳，防止平台判离线
                if value != last or now - last_heartbeat >= config.RELAY_HEARTBEAT:
                    props = relay._props_from_value(value)
                    mqtt.publish_json(client, report_topic,
                                      jetlinks.report_property_payload(props),
                                      qos=config.MQTT_QOS)
                    if value != last:
                        log.info("开关状态变化，已上报：%s", props)
                    last = value
                    last_heartbeat = now
            except Exception as e:
                log.warning("读取继电器状态失败：%s", e)
            stop_event.wait(config.RELAY_POLL_INTERVAL)
    finally:
        client.publish(offline_topic, payload=json.dumps(jetlinks.offline_payload()), qos=config.MQTT_QOS)
        client.loop_stop()
        client.disconnect()
        relay.close()
        log.info("已下线，退出")


def main():
    parser = argparse.ArgumentParser(description="8 路继电器模拟器（JetLinks 物模型）")
    parser.add_argument("--read", action="store_true", help="只读取一次 8 路状态并上报后退出")
    parser.add_argument("--set", nargs=2, metavar=("CHANNEL", "VALUE"),
                        help="写某一路开关，例如 --set 3 1（开第 3 路）")
    args = parser.parse_args()

    if args.set is not None:
        channel = int(args.set[0])
        value = _to_switch(args.set[1])
        relay = RelayDevice()
        try:
            relay.connect()
            relay.write_channel(channel, value)
        finally:
            relay.close()
        return

    if args.read:
        relay = RelayDevice()
        product_id = config.RELAY_PRODUCT_ID
        report_topic = jetlinks.report_property_topic(product_id, relay.device_id)
        client = mqtt.create_client(relay.device_id)
        mqtt.connect(client, config.MQTT_BROKER, config.MQTT_PORT,
                     config.MQTT_KEEPALIVE, config.MQTT_USERNAME, config.MQTT_PASSWORD)
        try:
            relay.connect()
            props = relay.read_channels()
            mqtt.publish_json(client, report_topic,
                              jetlinks.report_property_payload(props),
                              qos=config.MQTT_QOS)
            log.info("已上报 8 路开关状态：%s", props)
        finally:
            client.loop_stop()
            client.disconnect()
            relay.close()
        return

    run_poll()


if __name__ == "__main__":
    main()
