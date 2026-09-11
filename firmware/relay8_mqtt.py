# -*- coding: utf-8 -*-
"""
8 路继电器 JetLinks 物模型固件 (ESP32-C3 / MicroPython v1.24)
============================================================
功能:
  1. 两种模式:配网模式(AP 热点 + 网页) / 正常运行模式(WiFi STA + MQTT)
  2. 板载按键 SW1~SW4:短按切换对应继电器 1~4;SW1 长按 5 秒进配网模式(热点 IP 192.168.4.1)
  3. 配置持久化到 config.json,断电重启自动用已存配置联网并推送
  4. MQTT 断线自动重连,重连后重新订阅并补发上线 + 全量状态

硬件引脚(按实物核对,集中在此方便修改):
  RELAY_PINS = [3, 4, 5, 7, None, None, None, None]
    - 前 4 个是真实继电器(GPIO3/4/5/7,低电平吸合:0=开 1=关)
    - 后 4 个 None = 虚拟通道(无物理继电器,仅记录/上报状态,满足平台 8 路)
  BTN_PINS = [10, 9, 6, 8]  (SW1~SW4,上拉输入,按下为低;短按切换继电器1~4,SW1长按进配网)

JetLinks 物模型 MQTT 协议:
  上报 /{productId}/{deviceId}/properties/report
        payload: {"messageId":"...","properties":{"switch_1":0,...,"switch_8":0}}
  订阅 /{productId}/{deviceId}/properties/read | write
  回复 /{productId}/{deviceId}/properties/read/reply | write/reply
  订阅 /{productId}/{deviceId}/function/invoke(平台功能调用,控制继电器)
  回复 /{productId}/{deviceId}/function/invoke/reply
  上线 /{productId}/{deviceId}/online

部署:保存到板子为 main.py 开机自启,或用 Thonny 直接运行。
"""
import json
import os
import socket
import struct
import time

import ubinascii
import network
from machine import Pin, reset

CONFIG_PATH = "config.json"

# ---------- 硬件引脚(按实物修改这里) ----------
RELAY_PINS = [3, 4, 5, 7, None, None, None, None]   # 4 路真实 + 4 路虚拟
CHANNELS = len(RELAY_PINS)                           # 8
BTN_PINS = [10, 9, 6, 8]                             # SW1~SW4(按下为低,短按切换继电器)

# ---------- 运行参数 ----------
AP_SSID = "Relay8-Setup"
WIFI_TIMEOUT_S = 30        # 上电连 WiFi 超时(秒),超时进配网
LONG_PRESS_MS = 5000       # SW1 长按进配网阈值
HEARTBEAT_S = 30           # 定时上报状态(心跳),防止平台判离线
MQTT_PING_S = 20           # MQTT PINGREQ 周期(keepalive=60)
RETRY_S = 5                # WiFi / MQTT 断线重连周期

# 默认配置(空值首次使用时在配网页填写)
DEFAULT_CONFIG = {
    "wifi_ssid": "",
    "wifi_password": "",
    "mqtt_host": "",
    "mqtt_port": 9783,
    "mqtt_user": "",
    "mqtt_password": "",
    "product_id": "",
    "device_id": "",        # 空 = 自动读取设备 MAC
    # ---- Modbus-TCP 网关采集(主机) ----
    "modbus_host": "",      # 电脑端从站 IP(空 = 不采集)
    "modbus_port": 5502,
    "modbus_unit": 1,       # 从站单元 ID
    "modbus_poll_s": 5,     # 采集周期(秒),可 5~120
    "modbus_regs": '[{"addr":0,"key":"temperature","scale":0.1},'
                   '{"addr":1,"key":"humidity","scale":0.1}]',
}

# ---------- 全局状态 ----------
relays = [Pin(n, Pin.OUT, value=1) if n is not None else None for n in RELAY_PINS]
virt_state = [0] * CHANNELS          # 虚拟通道当前状态(switch_5~8)
relay_off_at = [0] * CHANNELS        # 自动断开到期时刻(ticks_ms),0=无定时
btns = [Pin(p, Pin.IN, Pin.PULL_UP) for p in BTN_PINS]   # SW1~SW4

wlan = network.WLAN(network.STA_IF)
client = None
TOPIC_REPORT = TOPIC_READ = TOPIC_WRITE = ""
TOPIC_READ_REPLY = TOPIC_WRITE_REPLY = TOPIC_ONLINE = ""
TOPIC_FUNC = TOPIC_FUNC_REPLY = ""


# ---------- 配置持久化 ----------
def mac_hex():
    try:
        wlan.active(True)
        return ubinascii.hexlify(wlan.config("mac")).decode()
    except Exception:
        return "esp32c3"


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULT_CONFIG)
    out = dict(DEFAULT_CONFIG)
    out.update({k: cfg[k] for k in DEFAULT_CONFIG if k in cfg})
    return out


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)


# ---------- 继电器统一入口 ----------
def relay_is_on(i):
    if relays[i] is None:
        return virt_state[i] == 1
    return relays[i].value() == 0      # 低电平吸合


def state_props():
    return {"switch_%d" % (i + 1): (1 if relay_is_on(i) else 0)
            for i in range(CHANNELS)}


def _apply_relay(i, on, duration_s=0):
    """改继电器状态(不主动上报),所有控制来源统一走这里。"""
    if relays[i] is None:
        virt_state[i] = 1 if on else 0
    else:
        relays[i].value(0 if on else 1)
    if on and duration_s > 0:
        relay_off_at[i] = time.ticks_add(time.ticks_ms(), duration_s * 1000)
    else:
        relay_off_at[i] = 0
    print("relay", i + 1, "ON" if on else "OFF")


def set_relay(i, on, duration_s=0):
    _apply_relay(i, on, duration_s)
    publish_state()


# ---------- 工具 ----------
def new_message_id():
    try:
        return ubinascii.hexlify(os.urandom(8)).decode()
    except Exception:
        return "%08x" % time.ticks_ms()


def _publish(topic, obj):
    if client is not None:
        try:
            client.publish(topic, json.dumps(obj), qos=1)
        except OSError:
            pass


def publish_state():
    _publish(TOPIC_REPORT, {"messageId": new_message_id(), "properties": state_props()})


def _to_switch(v):
    """把任意下发值归一化为 0/1。"""
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if v != 0 else 0
    return 1 if str(v).lower() in ("1", "on", "true", "yes", "开") else 0


def _prop_channel(prop_id):
    """switch_3 -> 3;不合法返回 None。"""
    if isinstance(prop_id, str) and prop_id.startswith("switch_"):
        try:
            n = int(prop_id[7:])
            return n if 1 <= n <= CHANNELS else None
        except ValueError:
            return None
    return None


# ---------- Modbus-TCP 主机(网关采集) ----------
def _recv_exact(s, n):
    """可靠读满 n 字节;超时/断开返回 None。"""
    buf = b""
    while len(buf) < n:
        try:
            chunk = s.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def modbus_read_regs(host, port, unit, addr, count, timeout=1.5):
    """读保持寄存器(FC 0x03)。短超时,失败/超时返回 None,不阻塞主循环。"""
    s = None
    try:
        ai = socket.getaddrinfo(host, port)[0]
        s = socket.socket()
        s.settimeout(timeout)
        s.connect(ai[-1])
        pdu = struct.pack(">BHH", 0x03, addr, count)
        s.send(struct.pack(">HHHB", 1, 0, len(pdu) + 1, unit) + pdu)
        hdr = _recv_exact(s, 7)
        if hdr is None or len(hdr) < 7:
            return None
        _, _, length, _ = struct.unpack(">HHHB", hdr)
        rest = _recv_exact(s, length - 1)
        if rest is None or len(rest) < 2 or rest[0] != 0x03:
            return None
        nbytes = rest[1]
        data = rest[2:2 + nbytes]
        return [struct.unpack(">H", data[i:i + 2])[0]
                for i in range(0, len(data) - 1, 2)]
    except Exception:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def parse_modbus_regs(cfg):
    """解析寄存器配置 JSON,返回 [{addr,key,scale}];失败返回 []。"""
    try:
        arr = json.loads(cfg.get("modbus_regs") or "[]")
    except ValueError:
        return []
    out = []
    if not isinstance(arr, list):
        return []
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            addr = int(item.get("addr", 0))
            key = str(item.get("key", ""))
            scale = float(item.get("scale", 1))
        except (TypeError, ValueError):
            continue
        if key and 0 <= addr <= 0xFFFF:
            out.append({"addr": addr, "key": key, "scale": scale})
    return out


def modbus_poll(cfg):
    """采集一次:读配置的寄存器(合并连续地址),返回 {key: 值};失败返回 None。"""
    regs = parse_modbus_regs(cfg)
    if not regs:
        return None
    try:
        host = cfg["modbus_host"]
        port = int(cfg["modbus_port"])
        unit = int(cfg["modbus_unit"])
    except (KeyError, ValueError):
        return None
    regs = sorted(regs, key=lambda r: r["addr"])
    out = {}
    i = 0
    while i < len(regs):
        j = i
        while j + 1 < len(regs) and regs[j + 1]["addr"] == regs[j]["addr"] + 1:
            j += 1
        start = regs[i]["addr"]
        count = j - i + 1
        vals = modbus_read_regs(host, port, unit, start, count)
        if vals is None or len(vals) < count:
            return None
        for k in range(count):
            out[regs[i + k]["key"]] = round(vals[k] * regs[i + k]["scale"], 2)
        i = j + 1
    return out


# ---------- 配网网页 ----------
PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>继电器配置</title></head>
<body style="font-family:sans-serif;max-width:460px;margin:20px auto">
<h2>8 路继电器模块配置</h2>
<p>保存后设备自动重启并联网推送。</p>
<form method="POST" action="/save">
WiFi 名称(2.4GHz):<input name="wifi_ssid" value="{wifi_ssid}"><br><br>
WiFi 密码:<input name="wifi_password" value="{wifi_password}"><br><br>
MQTT 服务器地址:<input name="mqtt_host" value="{mqtt_host}"><br><br>
MQTT 端口:<input name="mqtt_port" value="{mqtt_port}"><br><br>
MQTT 用户名:<input name="mqtt_user" value="{mqtt_user}"><br><br>
MQTT 密码:<input name="mqtt_password" value="{mqtt_password}"><br><br>
产品 ID(productId):<input name="product_id" value="{product_id}"><br><br>
设备 ID(留空=用 MAC):<input name="device_id" value="{device_id}"><br><br>
<hr>Modbus 采集(可选,留空服务器地址=不采集)<br>
服务器地址(电脑 IP):<input name="modbus_host" value="{modbus_host}"><br><br>
端口:<input name="modbus_port" value="{modbus_port}"><br><br>
单元 ID:<input name="modbus_unit" value="{modbus_unit}"><br><br>
采集周期(秒,5~120):<input name="modbus_poll_s" value="{modbus_poll_s}"><br><br>
寄存器配置(JSON):<input name="modbus_regs" value="{modbus_regs}"><br><br>
<button style="padding:8px 20px">保存并重启</button>
</form></body></html>"""


def unquote_plus(s):
    out = bytearray()
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "+":
            out.append(0x20)
            i += 1
        elif c == "%" and i + 2 < n:
            try:
                out.append(int(s[i + 1:i + 3], 16))
                i += 3
            except ValueError:
                out.append(0x25)
                i += 1
        else:
            out += c.encode("utf-8")
            i += 1
    return out.decode("utf-8", "replace")


def parse_form(body):
    out = {}
    for kv in body.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[unquote_plus(k)] = unquote_plus(v)
    return out


def http_send(conn, status, body, ctype="text/plain"):
    conn.send("HTTP/1.0 {}\r\nContent-Type: {}; charset=utf-8\r\n\r\n{}"
              .format(status, ctype, body))
    conn.close()


def render_page(cfg):
    d = {k: str(cfg[k]).replace('"', "&quot;") for k in DEFAULT_CONFIG}
    if not d["device_id"]:
        d["device_id"] = mac_hex()
    return PAGE.format(**d)


def portal(cfg):
    global client
    for r in relays:
        if r is not None:
            r.value(1)                       # 配网模式继电器全关
    for i in range(CHANNELS):
        virt_state[i] = 0
        relay_off_at[i] = 0
    if client is not None:
        try:
            client.disconnect()
        except Exception:
            pass
        client = None
    sta = network.WLAN(network.STA_IF)
    sta.active(False)
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=AP_SSID)                 # 开放热点
    print("配网模式:手机连接热点", AP_SSID, "后打开 http://192.168.4.1")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 80))
    srv.listen(1)
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            continue
        conn.settimeout(2)
        try:
            req = conn.recv(2048)
            head, _, rest = req.partition(b"\r\n\r\n")
            parts = head.split(b"\r\n", 1)[0].decode().split()
            method, path = parts[0], parts[1]
            clen = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    clen = int(line.split(b":")[1])
            while len(rest) < clen:
                rest += conn.recv(1024)
        except (OSError, IndexError, ValueError):
            conn.close()
            continue
        if method == "GET" and path == "/":
            http_send(conn, "200 OK", render_page(cfg), "text/html")
        elif method == "POST" and path == "/save":
            form = parse_form(rest.decode("utf-8", "replace"))
            for k in ("wifi_ssid", "wifi_password", "mqtt_host",
                      "mqtt_user", "mqtt_password", "product_id", "device_id",
                      "modbus_host", "modbus_regs"):
                cfg[k] = form.get(k, "")
            cfg["mqtt_port"] = int(form.get("mqtt_port") or 9783)
            cfg["modbus_port"] = int(form.get("modbus_port") or 5502)
            cfg["modbus_unit"] = int(form.get("modbus_unit") or 1)
            cfg["modbus_poll_s"] = max(5, min(120, int(form.get("modbus_poll_s") or 5)))
            if not cfg["device_id"]:
                cfg["device_id"] = mac_hex()
            save_config(cfg)
            http_send(conn, "200 OK",
                      "已保存,设备重启中... 若 WiFi 连不上会回到此热点", "text/html")
            time.sleep(1)
            reset()
        else:
            http_send(conn, "404 Not Found", "404", "text/html")


# ---------- WiFi ----------
def wifi_connect(cfg, timeout_s):
    wlan.active(True)
    time.sleep(2)                            # active 后等 2 秒,否则偶发 Wifi Internal Error
    start = time.ticks_ms()
    while not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), start) > timeout_s * 1000:
            return False
        try:
            wlan.connect(cfg["wifi_ssid"], cfg["wifi_password"])
        except OSError:
            pass
        time.sleep(1)
    print("WiFi connected:", wlan.ifconfig()[0])
    return True


# ---------- MQTT ----------
def _set_topics(pid, did):
    global TOPIC_REPORT, TOPIC_READ, TOPIC_WRITE
    global TOPIC_READ_REPLY, TOPIC_WRITE_REPLY, TOPIC_ONLINE
    global TOPIC_FUNC, TOPIC_FUNC_REPLY
    TOPIC_REPORT = "/%s/%s/properties/report" % (pid, did)
    TOPIC_READ = "/%s/%s/properties/read" % (pid, did)
    TOPIC_WRITE = "/%s/%s/properties/write" % (pid, did)
    TOPIC_READ_REPLY = "/%s/%s/properties/read/reply" % (pid, did)
    TOPIC_WRITE_REPLY = "/%s/%s/properties/write/reply" % (pid, did)
    TOPIC_ONLINE = "/%s/%s/online" % (pid, did)
    TOPIC_FUNC = "/%s/%s/function/invoke" % (pid, did)
    TOPIC_FUNC_REPLY = "/%s/%s/function/invoke/reply" % (pid, did)


def _func_parse(inputs):
    """解析功能调用 inputs([{name,value}...]) -> (channel, state);失败返回 (None, None)。"""
    channel = None
    state = None
    if isinstance(inputs, list):
        for it in inputs:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name", ""))
            val = it.get("value")
            if name in ("channel", "ch", "relay", "index", "通道"):
                try:
                    channel = int(float(val))
                except (TypeError, ValueError):
                    channel = None
            elif name in ("state", "status", "on", "开关"):
                state = _to_switch(val)
    return channel, state


def on_cmd(topic_b, payload_b):
    topic = topic_b.decode() if isinstance(topic_b, bytes) else str(topic_b)
    try:
        msg = json.loads(payload_b.decode() if isinstance(payload_b, bytes) else str(payload_b))
    except (ValueError, AttributeError):
        return
    if not isinstance(msg, dict):
        return
    mid = msg.get("messageId") or ""
    props = msg.get("properties") or {}
    if topic == TOPIC_WRITE:
        applied = {}
        for k, v in props.items():
            ch = _prop_channel(k)
            if ch is None:
                continue
            on = _to_switch(v)
            _apply_relay(ch - 1, bool(on))
            applied[k] = on
        _publish(TOPIC_WRITE_REPLY, {"messageId": mid, "properties": applied, "success": True})
        if applied:
            publish_state()                  # 立即同步真实状态
        print("write cmd ->", applied)
    elif topic == TOPIC_READ:
        _publish(TOPIC_READ_REPLY, {"messageId": mid, "properties": state_props(), "success": True})
        print("read cmd replied")
    elif topic == TOPIC_FUNC:
        func = str(msg.get("function") or msg.get("functionId") or "").lower()
        if func in ("setswitch", "set_switch", "switch", "control"):
            ch, on = _func_parse(msg.get("inputs"))
            if ch is None or on is None or not (1 <= ch <= CHANNELS):
                _publish(TOPIC_FUNC_REPLY,
                         {"messageId": mid, "success": False, "output": "参数错误"})
                print("func cmd 参数错误 ->", msg.get("inputs"))
                return
            _apply_relay(ch - 1, bool(on))
            publish_state()
            _publish(TOPIC_FUNC_REPLY,
                     {"messageId": mid, "success": True, "output": True})
            print("func cmd -> relay %d %s" % (ch, "ON" if on else "OFF"))
        else:
            _publish(TOPIC_FUNC_REPLY,
                     {"messageId": mid, "success": False, "output": "未知功能:" + func})
            print("func cmd 未知功能 ->", func)


def mqtt_connect(cfg):
    global client
    from umqtt.simple import MQTTClient
    pid, did = cfg["product_id"], cfg["device_id"]
    _set_topics(pid, did)
    try:
        c = MQTTClient(did, cfg["mqtt_host"], int(cfg["mqtt_port"]),
                       user=cfg["mqtt_user"] or None,
                       password=cfg["mqtt_password"] or None,
                       keepalive=60)
        c.set_callback(on_cmd)
        c.connect()
        c.subscribe(TOPIC_READ, qos=1)
        c.subscribe(TOPIC_WRITE, qos=1)
        c.subscribe(TOPIC_FUNC, qos=1)
        client = c
        c.publish(TOPIC_ONLINE,
                  json.dumps({"messageId": new_message_id(),
                              "timestamp": int(time.time() * 1000)}), qos=1)
        publish_state()
        print("MQTT connected:", cfg["mqtt_host"], "device:", did)
        return True
    except OSError as e:
        print("MQTT connect failed:", e)
        client = None
        return False


# ---------- 正常运行模式 ----------
def run_normal(cfg):
    global client
    if not wifi_connect(cfg, WIFI_TIMEOUT_S):
        return "config"
    mqtt_connect(cfg)
    last_ping = last_hb = time.ticks_ms()
    mqtt_retry = wifi_retry = time.ticks_ms()
    modbus_last = time.ticks_ms()     # 上次 Modbus 采集时刻
    btn_last = [b.value() for b in btns]
    btn_down_at = [0] * len(btns)
    print("正常运行中, device_id=", cfg["device_id"])
    if cfg.get("modbus_host"):
        print("Modbus 采集: host=%s:%s unit=%s 周期=%ss regs=%s" % (
            cfg["modbus_host"], cfg["modbus_port"], cfg["modbus_unit"],
            cfg.get("modbus_poll_s", 5), cfg.get("modbus_regs")))
    else:
        print("Modbus 采集: 未启用(modbus_host 为空)")
    while True:
        now = time.ticks_ms()

        # 按键扫描:SW1 长按 5 秒进配网;四键短按分别切换继电器 1~4
        for i in range(len(btns)):
            st = btns[i].value()                      # 0=按下(低电平)
            if st == 0 and btn_last[i] == 1:          # 按下瞬间
                btn_down_at[i] = now
            if i == 0 and st == 0 and time.ticks_diff(now, btn_down_at[0]) >= LONG_PRESS_MS:
                print("长按 SW1,进入配网模式")
                return "config"
            if st == 1 and btn_last[i] == 0:          # 松开 -> 短按切换
                if i != 0 or time.ticks_diff(now, btn_down_at[i]) < LONG_PRESS_MS:
                    set_relay(i, not relay_is_on(i))
            btn_last[i] = st

        # 继电器自动断开定时
        for i in range(CHANNELS):
            if relay_off_at[i] and time.ticks_diff(now, relay_off_at[i]) >= 0:
                set_relay(i, False)

        if wlan.isconnected():
            # Modbus-TCP 采集(分时调度:按周期触发,短超时,不阻塞继电器/MQTT)
            if cfg.get("modbus_host") and time.ticks_diff(now, modbus_last) >= 0:
                modbus_last = time.ticks_add(now, int(cfg.get("modbus_poll_s", 5)) * 1000)
                try:
                    vals = modbus_poll(cfg)
                except Exception as e:
                    vals = None
                    print("Modbus 采集异常:", e)
                if vals is not None:
                    _publish(TOPIC_REPORT,
                             {"messageId": new_message_id(), "properties": vals})
                    print("Modbus 采集 ->", vals)
                else:
                    print("Modbus 采集失败(从站不可达/超时)")
            if cfg["mqtt_host"]:
                if client is None:
                    if time.ticks_diff(now, mqtt_retry) >= 0:
                        mqtt_retry = time.ticks_add(now, RETRY_S * 1000)
                        mqtt_connect(cfg)
                else:
                    try:
                        client.check_msg()
                        if time.ticks_diff(now, last_ping) >= MQTT_PING_S * 1000:
                            client.ping()
                            last_ping = now
                        if time.ticks_diff(now, last_hb) >= HEARTBEAT_S * 1000:
                            publish_state()
                            last_hb = now
                    except OSError:
                        print("MQTT 断开,稍后重连")
                        client = None
        else:
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    pass
                client = None
            if time.ticks_diff(now, wifi_retry) >= 0:
                wifi_retry = time.ticks_add(now, RETRY_S * 1000)
                print("WiFi 断开,重连中...")
                try:
                    wlan.connect(cfg["wifi_ssid"], cfg["wifi_password"])
                except OSError:
                    pass
        time.sleep_ms(50)


# ---------- 入口 ----------
def main():
    cfg = load_config()
    if not cfg["device_id"]:
        cfg["device_id"] = mac_hex()
        save_config(cfg)
    if not cfg["wifi_ssid"] or run_normal(cfg) == "config":
        portal(cfg)


main()
