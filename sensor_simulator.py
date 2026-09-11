# -*- coding: utf-8 -*-
"""
温湿度模拟器（JetLinks 物模型接入）
==============================================
职责：
  1. 监视 sensor.json 中的温湿度数值，变化时按 JetLinks 物模型格式上报；
  2. 订阅 JetLinks 下发 topic（read/write/function），收到指令后改写 sensor.json 并回复。

测试方式：
  1. 运行 python sensor_simulator.py
  2. 手动改 sensor.json 数值 -> 脚本检测到变化 -> 上报 /properties/report
  3. 在 JetLinks 平台点「修改属性」/「调用功能」按钮 -> 下发 write/function 指令
     -> 脚本 on_message 回调收到 -> 改写 sensor.json -> 回复平台

上报 topic：/{productId}/{deviceId}/properties/report
下发 topic：/{productId}/{deviceId}/properties/read | write、/function/invoke
"""
import json
import logging
import os
import random
import signal
import threading
import time
from datetime import datetime

import config
import jetlinks
import mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("sensor")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, config.SENSOR_JSON_FILE)

PROP_TEMP = config.SENSOR_PROP_TEMP
PROP_HUM = config.SENSOR_PROP_HUM

DEFAULT_STATE = {PROP_TEMP: 25.0, PROP_HUM: 50.0}

# 物模型「功能标识」，需与 JetLinks 平台定义的功能标识一致
FUNCTION_RANDOMIZE = "randomize"


# ---------- sensor.json 读写 ----------
def _ensure_json_exists():
    """若 sensor.json 不存在，写入默认初始值。"""
    if os.path.exists(DATA_FILE):
        return
    _write_json(DEFAULT_STATE)
    log.info("已生成默认 JSON 文件：%s（可手动修改其中数值进行测试）", DATA_FILE)


def _read_json():
    """读取 sensor.json 内容，失败返回 None。"""
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("读取 %s 失败：%s", DATA_FILE, e)
        return None


def _write_json(data):
    """原子写入 sensor.json。"""
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def _current_properties():
    """从 sensor.json 读取物模型属性值 {属性标识: 值}。"""
    data = _read_json() or {}
    return {PROP_TEMP: data.get(PROP_TEMP), PROP_HUM: data.get(PROP_HUM)}


def build_legacy_payload(props, seq):
    """构造旧版自定义 payload（发到 iot/group8/.../data，供 MQTTX 直测）。"""
    ts = time.time()
    return {
        "type": "sensor",
        "device_id": config.SENSOR_DEVICE_ID,
        "group": config.GROUP_ID,
        "temperature": props.get(PROP_TEMP),
        "humidity": props.get(PROP_HUM),
        "unit": {"temperature": "celsius", "humidity": "percentRH"},
        "seq": seq,
        "ts": round(ts, 3),
        "datetime": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


# ---------- 下发指令处理 ----------
def _handle_read(client, reply_topic, payload):
    message_id = payload.get("messageId")
    props = _current_properties()
    client.publish(reply_topic, json.dumps(jetlinks.read_property_reply_payload(message_id, props),
                                           ensure_ascii=False), qos=config.MQTT_QOS)
    log.info("收到读取指令，已回复：%s", props)


def _handle_write(client, reply_topic, payload):
    message_id = payload.get("messageId")
    new_props = payload.get("properties", {}) or {}
    # 改写 sensor.json（只更新物模型里定义的属性）
    data = _read_json() or {}
    for k, v in new_props.items():
        if k in (PROP_TEMP, PROP_HUM):
            data[k] = v
    _write_json(data)
    client.publish(reply_topic, json.dumps(jetlinks.write_property_reply_payload(message_id, new_props),
                                           ensure_ascii=False), qos=config.MQTT_QOS)
    log.info("收到写指令，已改写 sensor.json：%s", new_props)


def _handle_function(client, reply_topic, report_topic, payload):
    message_id = payload.get("messageId")
    function_id = payload.get("function")
    inputs = payload.get("inputs", []) or []
    args = {item.get("name"): item.get("value") for item in inputs}

    if function_id == FUNCTION_RANDOMIZE:
        data = _read_json() or {}
        data[PROP_TEMP] = round(random.uniform(float(args.get("min", -10)), float(args.get("max", 50))), 1)
        data[PROP_HUM] = round(random.uniform(float(args.get("hmin", 0)), float(args.get("hmax", 100))), 1)
        _write_json(data)
        output = {PROP_TEMP: data[PROP_TEMP], PROP_HUM: data[PROP_HUM]}
        success = True
        log.info("收到功能调用 %s，已生成新值：%s", function_id, output)
    else:
        output = f"未知功能：{function_id}"
        success = False
        log.warning("收到未知功能调用：%s", function_id)

    client.publish(reply_topic, json.dumps(jetlinks.invoke_function_reply_payload(message_id, output, success),
                                           ensure_ascii=False), qos=config.MQTT_QOS)
    # 功能调用后立即补发一次属性上报，让平台马上看到控制后的最新值）
    if success:
        client.publish(report_topic, json.dumps(jetlinks.report_property_payload(_current_properties()),
                                                ensure_ascii=False), qos=config.MQTT_QOS)
        log.info("功能调用后已补发状态上报")


def _make_on_message(client, product_id, device_id):
    """构造 on_message 回调，根据下发 topic 分发处理。"""
    read_topic = jetlinks.read_property_topic(product_id, device_id)
    write_topic = jetlinks.write_property_topic(product_id, device_id)
    invoke_topic = jetlinks.invoke_function_topic(product_id, device_id)
    read_reply = jetlinks.read_property_reply_topic(product_id, device_id)
    write_reply = jetlinks.write_property_reply_topic(product_id, device_id)
    invoke_reply = jetlinks.invoke_function_reply_topic(product_id, device_id)
    report_topic = jetlinks.report_property_topic(product_id, device_id)

    def on_message(c, userdata, msg):
        payload = jetlinks.parse_downlink(msg.payload)
        if payload is None:
            log.warning("无法解析下发消息：%s", msg.payload)
            return
        try:
            if msg.topic == read_topic:
                _handle_read(client, read_reply, payload)
            elif msg.topic == write_topic:
                _handle_write(client, write_reply, payload)
            elif msg.topic == invoke_topic:
                _handle_function(client, invoke_reply, report_topic, payload)
            else:
                log.warning("未知下发 topic：%s", msg.topic)
        except Exception as e:
            log.error("处理下发消息出错：%s", e)

    return on_message


# ---------- 主循环 ----------
def run():
    _ensure_json_exists()

    product_id = config.SENSOR_PRODUCT_ID
    device_id = config.SENSOR_DEVICE_ID

    report_topic = jetlinks.report_property_topic(product_id, device_id)
    read_topic = jetlinks.read_property_topic(product_id, device_id)
    write_topic = jetlinks.write_property_topic(product_id, device_id)
    invoke_topic = jetlinks.invoke_function_topic(product_id, device_id)
    online_topic = jetlinks.online_topic(product_id, device_id)
    offline_topic = jetlinks.offline_topic(product_id, device_id)

    legacy_data_topic = config.sensor_data_topic(device_id)
    legacy_status_topic = config.sensor_status_topic(device_id)

    client = mqtt.create_client(device_id, will_topic=legacy_status_topic)
    client.on_message = _make_on_message(client, product_id, device_id)
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

    def _handle_signal(signum, frame):
        log.info("收到退出信号，正在下线...")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("温湿度模拟器启动 device=%s", device_id)
    log.info("上报主题：%s", report_topic)
    log.info("订阅下发：%s / %s / %s", read_topic, write_topic, invoke_topic)

    last = None
    seq = 0
    last_heartbeat = 0.0
    try:
        while not stop_event.is_set():
            props = _current_properties()
            current = (props.get(PROP_TEMP), props.get(PROP_HUM))
            # 变化时上报；同时按心跳周期定时补报一次作为心跳，
            # 避免 MQTT客户端接入模式下平台因长时间无消息把设备判为离线。
            now = time.time()
            if current != last or now - last_heartbeat >= config.SENSOR_HEARTBEAT:
                seq += 1
                client.publish(report_topic, json.dumps(jetlinks.report_property_payload(props),
                                                        ensure_ascii=False), qos=config.MQTT_QOS)
                mqtt.publish_json(client, legacy_data_topic, build_legacy_payload(props, seq),
                                  qos=config.MQTT_QOS)
                if current != last:
                    log.info("数值变化，已上报：%s", props)
                last = current
                last_heartbeat = now
            stop_event.wait(config.SENSOR_INTERVAL)
    finally:
        client.publish(offline_topic, payload=json.dumps(jetlinks.offline_payload()), qos=config.MQTT_QOS)
        mqtt.publish_status(client, legacy_status_topic, "offline")
        client.loop_stop()
        client.disconnect()
        log.info("已下线，退出")


def main():
    run()


if __name__ == "__main__":
    main()
