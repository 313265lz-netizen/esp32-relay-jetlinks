# -*- coding: utf-8 -*-
"""
JetLinks 物模型 MQTT 协议封装
==============================
实现 JetLinks 官方协议的上报 / 下发 topic 与 payload 格式，供两个模拟终端共用。

topic 格式（带前导斜杠，{productId}/{deviceId} 需与 JetLinks 平台一致）：

  设备上报：
    /{productId}/{deviceId}/properties/report        上报属性
    /{productId}/{deviceId}/properties/read/reply    读取属性回复
    /{productId}/{deviceId}/properties/write/reply   修改属性回复
    /{productId}/{deviceId}/function/invoke/reply    功能调用回复
    /{productId}/{deviceId}/event/{eventId}          事件上报

  平台下发（设备需订阅）：
    /{productId}/{deviceId}/properties/read          读取属性
    /{productId}/{deviceId}/properties/write         修改属性
    /{productId}/{deviceId}/function/invoke          调用功能

  上下线：
    /{productId}/{deviceId}/online / offline
"""
import json
import time
import uuid


def _mk(product_id, device_id, suffix):
    return f"/{product_id}/{device_id}/{suffix}"


# ---------- topic ----------
def report_property_topic(product_id, device_id):
    return _mk(product_id, device_id, "properties/report")


def read_property_topic(product_id, device_id):
    return _mk(product_id, device_id, "properties/read")


def read_property_reply_topic(product_id, device_id):
    return _mk(product_id, device_id, "properties/read/reply")


def write_property_topic(product_id, device_id):
    return _mk(product_id, device_id, "properties/write")


def write_property_reply_topic(product_id, device_id):
    return _mk(product_id, device_id, "properties/write/reply")


def invoke_function_topic(product_id, device_id):
    return _mk(product_id, device_id, "function/invoke")


def invoke_function_reply_topic(product_id, device_id):
    return _mk(product_id, device_id, "function/invoke/reply")


def event_topic(product_id, device_id, event_id):
    return _mk(product_id, device_id, f"event/{event_id}")


def online_topic(product_id, device_id):
    return _mk(product_id, device_id, "online")


def offline_topic(product_id, device_id):
    return _mk(product_id, device_id, "offline")


# ---------- 工具 ----------
def now_ms():
    """毫秒时间戳。"""
    return int(time.time() * 1000)


def new_message_id():
    """生成随机 messageId。"""
    return uuid.uuid4().hex[:16]


# ---------- payload 构造（设备 -> 平台） ----------
def report_property_payload(properties):
    """设备上报属性。"""
    return {"messageId": new_message_id(), "properties": properties}


def online_payload():
    """设备上线消息 payload（JSON，带 timestamp）。"""
    return {"messageId": new_message_id(), "timestamp": now_ms()}


def offline_payload():
    """设备下线消息 payload（JSON，带 timestamp）。"""
    return {"messageId": new_message_id(), "timestamp": now_ms()}


def read_property_reply_payload(message_id, properties):
    """读取属性回复。"""
    return {"messageId": message_id, "properties": properties, "success": True}


def write_property_reply_payload(message_id, properties):
    """修改属性回复。"""
    return {"messageId": message_id, "properties": properties, "success": True}


def invoke_function_reply_payload(message_id, output=None, success=True):
    """功能调用回复。"""
    return {"messageId": message_id, "output": output, "success": success}


# ---------- payload 解析（平台 -> 设备） ----------
def parse_downlink(payload_bytes):
    """解析平台下发的 JSON 消息，失败返回 None。"""
    try:
        return json.loads(payload_bytes.decode("utf-8"))
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        return None
