# -*- coding: utf-8 -*-
"""
MQTT 连接与发布工具
===================
基于 paho-mqtt，封装了连接、在线/离线遗嘱、JSON 发布等常用逻辑。
连接会真正等待服务器确认（CONNACK），认证失败时直接报错，避免"假连接"。
"""
import json
import logging
import threading

import paho.mqtt.client as mqtt

log = logging.getLogger("mqtt")


def create_client(client_id, will_topic=None):
    """创建 MQTT 客户端。若提供 will_topic，则注册离线遗嘱消息。"""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)

    # 用于同步等待 CONNACK 结果
    client._conn_result = None
    client._conn_event = threading.Event()

    def _on_connect(c, userdata, flags, reason_code, properties):
        c._conn_result = reason_code
        c._conn_event.set()
        if reason_code == 0:
            log.info("MQTT 已连接（收到 CONNACK）")
        else:
            log.error("MQTT 连接被服务器拒绝：%s", reason_code)

    def _on_disconnect(c, userdata, disconnect_flags, reason_code, properties):
        log.warning("MQTT 连接已断开：%s", reason_code)

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    if will_topic:
        client.will_set(will_topic, payload="offline", qos=1, retain=True)
    return client


def connect(client, broker, port, keepalive=60, username="", password="", timeout=5):
    """连接 MQTT 服务器，并等待连接结果。认证失败会抛出异常。"""
    if username:
        client.username_pw_set(username, password)
    try:
        client.connect(broker, port, keepalive)
    except Exception as e:
        raise ConnectionError(f"无法连接 MQTT {broker}:{port}：{e}") from e

    client.loop_start()
    if not client._conn_event.wait(timeout):
        raise ConnectionError(f"MQTT 连接超时 {broker}:{port}")

    if client._conn_result != 0:
        raise ConnectionError(
            f"MQTT 连接被拒绝 {broker}:{port}，原因：{client._conn_result}。"
            f"请检查用户名/密码（当前 username='{username or '（空）'}'）"
        )
    log.info("已连接 MQTT %s:%s", broker, port)


def publish_json(client, topic, payload, qos=1, retain=False):
    """将 dict 序列化为 JSON 后发布到指定主题。"""
    data = json.dumps(payload, ensure_ascii=False)
    info = client.publish(topic, data, qos=qos, retain=retain)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        log.warning("发布失败 topic=%s rc=%s（未连接到 broker）", topic, info.rc)
    return info


def publish_status(client, topic, state):
    """发布在线状态（online/offline），保留消息。"""
    info = client.publish(topic, payload=state, qos=1, retain=True)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        log.warning("发布状态失败 topic=%s rc=%s（未连接到 broker）", topic, info.rc)
    return info
