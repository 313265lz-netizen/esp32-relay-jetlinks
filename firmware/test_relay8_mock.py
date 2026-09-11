# -*- coding: utf-8 -*-
"""
relay8_mqtt.py 离线逻辑测试(mock 硬件,不需要 ESP32)
运行:python test_relay8_mock.py
"""
import sys
import types
import time as _rtime
import binascii

# ---------- mock machine ----------
machine = types.ModuleType("machine")


class FakePin:
    IN = "IN"; OUT = "OUT"; PULL_UP = "PULL_UP"

    def __init__(self, n, mode=None, value=None, pull=None):
        self.n = n
        self.val = value if value is not None else 1

    def value(self, v=None):
        if v is None:
            return self.val
        self.val = v


machine.Pin = FakePin
machine.reset = lambda: (_ for _ in ()).throw(SystemExit("reset"))
sys.modules["machine"] = machine

# ---------- mock network ----------
network = types.ModuleType("network")
network.STA_IF = 0
network.AP_IF = 1


class FakeWLAN:
    _mac = b"\x7c\x4f\xad\x77\x75\xb0"

    def __init__(self, iface):
        self.iface = iface
        self._active = False
        self._connected = False

    def active(self, a=None):
        if a is not None:
            self._active = a
        return self._active

    def config(self, key):
        return self._mac if key == "mac" else None

    def isconnected(self):
        return self._connected

    def ifconfig(self):
        return ("192.168.1.10", "255.255.255.0", "192.168.1.1")

    def connect(self, *a):
        self._connected = True


network.WLAN = FakeWLAN
sys.modules["network"] = network

# ---------- mock ubinascii ----------
ubinascii = types.ModuleType("ubinascii")
ubinascii.hexlify = binascii.hexlify
sys.modules["ubinascii"] = ubinascii

# ---------- mock time (micropython ticks) ----------
mtime = types.ModuleType("time")
_t0 = _rtime.time()
mtime.ticks_ms = lambda: int((_rtime.time() - _t0) * 1000)
mtime.ticks_add = lambda a, b: a + b
mtime.ticks_diff = lambda a, b: a - b
mtime.sleep_ms = lambda ms: None
mtime.sleep = lambda s: None
mtime.time = _rtime.time
mtime.monotonic = _rtime.monotonic
sys.modules["time"] = mtime

# ---------- mock umqtt.simple ----------
umqtt = types.ModuleType("umqtt")
simple = types.ModuleType("umqtt.simple")


class FakeMQTTClient:
    def __init__(self, *a, **k):
        self.pub = []
        self.subs = []
        self.cb = None

    def set_callback(self, cb):
        self.cb = cb

    def connect(self, *a, **k):
        pass

    def subscribe(self, t, qos=0):
        self.subs.append(t)

    def publish(self, t, m, retain=False, qos=0):
        self.pub.append((t, m))

    def ping(self):
        pass

    def check_msg(self):
        pass

    def disconnect(self):
        pass


simple.MQTTClient = FakeMQTTClient
umqtt.simple = simple
sys.modules["umqtt"] = umqtt
sys.modules["umqtt.simple"] = simple

# ---------- 载入固件源码(去掉最后的 main() 调用) ----------
src = open("relay8_mqtt.py", encoding="utf-8").read()
src = src.replace("main()\n", "# main() removed for test\n")
ns = {}
exec(compile(src, "relay8_mqtt.py", "exec"), ns)

import json

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS", name)
    else:
        failed += 1
        print("  FAIL", name, detail)


print("== 1. 配置读写 ==")
ns["save_config"]({"wifi_ssid": "ap", "mqtt_port": 9783, "device_id": "d1"})
cfg = ns["load_config"]()
check("读回 device_id", cfg["device_id"] == "d1", cfg)

print("== 2. MAC 读取 ==")
mac = ns["mac_hex"]()
check("MAC = 7c4fad7775b0", mac == "7c4fad7775b0", mac)

print("== 3. 状态属性 ==")
props = ns["state_props"]()
check("8 个 switch", len(props) == 8, props)
check("switch_1 初始为 0", props["switch_1"] == 0)
check("键为 switch_1..8", list(props.keys()) == ["switch_%d" % i for i in range(1, 9)], props)

print("== 4. 继电器动作(真实 + 虚拟) ==")
ns["set_relay"](0, True)
check("真实通道1 开", ns["relay_is_on"](0) is True)
check("GPIO3 低电平", ns["relays"][0].value() == 0)
ns["set_relay"](4, True)   # 虚拟通道5
check("虚拟通道5 开", ns["relay_is_on"](4) is True)
check("虚拟通道5 无物理引脚", ns["relays"][4] is None)
ns["set_relay"](0, False)
ns["set_relay"](4, False)

print("== 5. 值归一化 / 属性解析 ==")
check("_to_switch(1)", ns["_to_switch"](1) == 1)
check("_to_switch('on')", ns["_to_switch"]("on") == 1)
check("_to_switch('0')", ns["_to_switch"]("0") == 0)
check("_to_switch(0)", ns["_to_switch"](0) == 0)
check("_prop_channel switch_8 -> 8", ns["_prop_channel"]("switch_8") == 8)
check("_prop_channel switch_9 -> None", ns["_prop_channel"]("switch_9") is None)
check("_prop_channel bad -> None", ns["_prop_channel"]("x") is None)

print("== 6. URL 解码(中文 SSID) ==")
check("unquote 中文", ns["unquote_plus"]("%E4%B8%AD%E6%96%87") == "中文",
      ns["unquote_plus"]("%E4%B8%AD%E6%96%87"))
check("unquote + -> 空格", ns["unquote_plus"]("a+b") == "a b")
check("parse_form", ns["parse_form"]("a=1&b=2") == {"a": "1", "b": "2"})

print("== 7. MQTT 连接 + 上报 ==")
cfg7 = {"product_id": "prod1", "device_id": "dev1", "mqtt_host": "1.2.3.4",
        "mqtt_port": 9783, "mqtt_user": "test", "mqtt_password": "123"}
ok = ns["mqtt_connect"](cfg7)
check("连接成功", ok is True)
cl = ns["client"]
check("订阅 write+read", ns["TOPIC_WRITE"] in cl.subs and ns["TOPIC_READ"] in cl.subs, cl.subs)
topics_pub = [t for t, _ in cl.pub]
check("发布 online", ns["TOPIC_ONLINE"] in topics_pub, topics_pub)
check("发布 report", ns["TOPIC_REPORT"] in topics_pub, topics_pub)
check("topic 前缀 /prod1/dev1/",
      ns["TOPIC_REPORT"] == "/prod1/dev1/properties/report", ns["TOPIC_REPORT"])

print("== 8. 下发 write 命令 ==")
cl.pub.clear()
ns["on_cmd"](ns["TOPIC_WRITE"].encode(),
             json.dumps({"messageId": "m1", "properties": {"switch_3": 1}}).encode())
check("继电器3 已开", ns["relay_is_on"](2) is True)
pub_topics = [t for t, _ in cl.pub]
check("回复 write/reply", ns["TOPIC_WRITE_REPLY"] in pub_topics, pub_topics)
check("再次上报 report", ns["TOPIC_REPORT"] in pub_topics, pub_topics)
reply = [m for t, m in cl.pub if t == ns["TOPIC_WRITE_REPLY"]][0]
reply_obj = json.loads(reply)
check("reply success=true", reply_obj["success"] is True, reply_obj)
check("reply messageId=m1", reply_obj["messageId"] == "m1", reply_obj)
check("reply properties.switch_3=1", reply_obj["properties"]["switch_3"] == 1, reply_obj)

print("== 9. 下发 read 命令 ==")
cl.pub.clear()
ns["on_cmd"](ns["TOPIC_READ"].encode(),
             json.dumps({"messageId": "m2"}).encode())
check("回复 read/reply", ns["TOPIC_READ_REPLY"] in [t for t, _ in cl.pub])
rd = [m for t, m in cl.pub if t == ns["TOPIC_READ_REPLY"]][0]
check("read 返回 8 路", len(json.loads(rd)["properties"]) == 8, rd)

print("== 10. 非法报文不崩 ==")
cl.pub.clear()
ns["on_cmd"](ns["TOPIC_WRITE"].encode(), b"not-json")
ns["on_cmd"](ns["TOPIC_WRITE"].encode(), json.dumps({"properties": {"switch_9": 1}}).encode())
check("非法 JSON 无崩溃", True)

print("== 11. Modbus 寄存器解析 ==")
cfg_mod = {"modbus_regs": '[{"addr":0,"key":"temperature","scale":0.1},'
                          '{"addr":1,"key":"humidity","scale":0.1}]'}
regs = ns["parse_modbus_regs"](cfg_mod)
check("解析出 2 个寄存器", len(regs) == 2, regs)
check("addr/key/scale 正确", regs[0] == {"addr": 0, "key": "temperature", "scale": 0.1}, regs)
check("非法 JSON 返回空", ns["parse_modbus_regs"]({"modbus_regs": "xx"}) == [])
check("缺省返回空", ns["parse_modbus_regs"]({}) == [])

print("== 12. modbus_read_regs 真实报文 ==")
import socket as _sock
import struct as _struct
import threading as _thr


def _serve(port_box, ready):
    srv = _sock.socket()
    srv.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_box.append(srv.getsockname()[1])
    ready.set()
    conn, _ = srv.accept()
    conn.settimeout(2)
    try:
        buf = b""
        while len(buf) < 12:
            c = conn.recv(12 - len(buf))
            if not c:
                break
            buf += c
        tid, _pid, _len, unit = _struct.unpack(">HHHB", buf[:7])
        data = _struct.pack(">HH", 256, 500)
        resp_pdu = bytes([0x03, len(data)]) + data
        conn.send(_struct.pack(">HHHB", tid, 0, len(resp_pdu) + 1, unit) + resp_pdu)
    finally:
        conn.close()
        srv.close()


_port = []
_ready = _thr.Event()
_thr.Thread(target=_serve, args=(_port, _ready), daemon=True).start()
_ready.wait(2)
vals = ns["modbus_read_regs"]("127.0.0.1", _port[0], 1, 0, 2)
check("读到 [256, 500]", vals == [256, 500], vals)

print("== 13. modbus_poll 采集(打桩 read) ==")
_orig_read = ns["modbus_read_regs"]
ns["modbus_read_regs"] = lambda h, p, u, a, c: [256, 500] if (a, c) == (0, 2) else None
_res = ns["modbus_poll"]({"modbus_host": "x", "modbus_port": 5502, "modbus_unit": 1,
                          "modbus_regs": cfg_mod["modbus_regs"]})
check("采集 temperature=25.6", _res is not None and _res.get("temperature") == 25.6, _res)
check("采集 humidity=50.0", _res is not None and _res.get("humidity") == 50.0, _res)
ns["modbus_read_regs"] = lambda h, p, u, a, c: None
check("从站超时返回 None", ns["modbus_poll"]({"modbus_host": "x", "modbus_port": 5502,
                                              "modbus_unit": 1, "modbus_regs": cfg_mod["modbus_regs"]}) is None)
_calls = []


def _fake(h, p, u, a, c):
    _calls.append((a, c))
    return [1] * c


ns["modbus_read_regs"] = _fake
_r2 = ns["modbus_poll"]({"modbus_host": "x", "modbus_port": 5502, "modbus_unit": 1,
                         "modbus_regs": '[{"addr":0,"key":"a","scale":1},{"addr":5,"key":"b","scale":1}]'})
check("非连续地址两次读", _calls == [(0, 1), (5, 1)], _calls)
check("非连续采集两值", _r2 == {"a": 1, "b": 1}, _r2)
ns["modbus_read_regs"] = _orig_read

print("== 14. 功能调用(function/invoke)控制继电器 ==")
check("订阅 function/invoke", ns["TOPIC_FUNC"] in cl.subs, cl.subs)
check("_func_parse 解析 channel/state",
      ns["_func_parse"]([{"name": "channel", "value": 3},
                         {"name": "state", "value": 0}]) == (3, 0))
cl.pub.clear()
ns["on_cmd"](ns["TOPIC_FUNC"].encode(),
             json.dumps({"messageId": "f1", "function": "setSwitch",
                         "inputs": [{"name": "channel", "value": 2},
                                    {"name": "state", "value": True}]}).encode())
check("功能调用 继电器2 开", ns["relay_is_on"](1) is True)
ftopics = [t for t, _ in cl.pub]
check("回复 function/invoke/reply", ns["TOPIC_FUNC_REPLY"] in ftopics, ftopics)
fr = json.loads([m for t, m in cl.pub if t == ns["TOPIC_FUNC_REPLY"]][0])
check("功能回复 success=true", fr["success"] is True, fr)
check("功能回复 messageId=f1", fr["messageId"] == "f1", fr)
check("功能回复 output=true", fr["output"] is True, fr)
# JetLinks 实际下发用的是 functionId 键(不是 function)
ns["set_relay"](1, False)
cl.pub.clear()
ns["on_cmd"](ns["TOPIC_FUNC"].encode(),
             json.dumps({"messageId": "f1b", "functionId": "setSwitch",
                         "inputs": [{"name": "channel", "value": 2},
                                    {"name": "state", "value": True}]}).encode())
check("functionId 键 继电器2 开", ns["relay_is_on"](1) is True)
frb = json.loads([m for t, m in cl.pub if t == ns["TOPIC_FUNC_REPLY"]][0])
check("functionId 键 回复 success=true", frb["success"] is True, frb)
ns["set_relay"](1, False)
cl.pub.clear()
ns["on_cmd"](ns["TOPIC_FUNC"].encode(),
             json.dumps({"messageId": "f2", "function": "setSwitch",
                         "inputs": [{"name": "channel", "value": 9},
                                    {"name": "state", "value": True}]}).encode())
fr2 = json.loads([m for t, m in cl.pub if t == ns["TOPIC_FUNC_REPLY"]][0])
check("越界通道 success=false", fr2["success"] is False, fr2)
cl.pub.clear()
ns["on_cmd"](ns["TOPIC_FUNC"].encode(),
             json.dumps({"messageId": "f3", "function": "unknown"}).encode())
fr3 = json.loads([m for t, m in cl.pub if t == ns["TOPIC_FUNC_REPLY"]][0])
check("未知功能 success=false", fr3["success"] is False, fr3)
ns["set_relay"](1, False)  # 复位继电器2

print()
print("结果: %d 通过, %d 失败" % (passed, failed))
sys.exit(1 if failed else 0)
