# -*- coding: utf-8 -*-
"""
电脑端 Modbus-TCP 从站模拟器(原生 socket,零依赖)
================================================
作用:在电脑上虚拟"另一台设备"(温湿度传感器 + 8 路继电器),供 ESP32 网关主机
通过 WiFi 走 Modbus-TCP 采集。这样调试阶段不用加任何 RS485 硬件。

用法:
    python modbus_tcp_slave.py            # 默认监听 0.0.0.0:5502
    python modbus_tcp_slave.py 5503       # 指定端口

寄存器表(保持寄存器,可被网关读 / 写):
    0x0000  温度  值 = 实际×10,例如 256 = 25.6°C(缓慢漂移)
    0x0001  湿度  值 = 实际×10,例如 500 = 50.0%(缓慢漂移)
    0x0007  8 路继电器状态,低 8 位(bit0~bit7 对应第 1~8 路;1=开 0=关)

支持功能码:
    0x03 读保持寄存器、0x06 写单个寄存器(可写继电器寄存器 0x0007)

注意:启动后屏幕会打印本机 IP,把这个 IP 填到 ESP32 配网页的
「Modbus 服务器地址」一栏,端口填 5502,即可让板子来采集。
"""
import random
import socket
import struct
import threading
import time

REG_COUNT = 16  # 寄存器数量 0x0000~0x000F

# 全局寄存器表(后台线程会更新温度/湿度)
REGS = [0] * REG_COUNT
REGS[0] = 256   # 温度 25.6°C
REGS[1] = 500   # 湿度 50.0%
REGS[7] = 0     # 8 路继电器全关

LOG = True


def log(msg):
    if LOG:
        print("[slave] %s" % msg)


def recv_exact(conn, n):
    """可靠地读满 n 字节;连接断开/超时返回 None。"""
    buf = b""
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def handle_connection(conn, addr):
    conn.settimeout(30)
    try:
        while True:
            hdr = recv_exact(conn, 7)          # MBAP 头
            if hdr is None:
                break
            tid, _pid, length, unit = struct.unpack(">HHHB", hdr)
            if length < 2 or length > 260:
                break
            pdu = recv_exact(conn, length - 1)  # 剩余 = 功能码 + 数据
            if pdu is None:
                break
            fc = pdu[0]
            resp_pdu = _dispatch(fc, pdu)
            resp = struct.pack(">HHHB", tid, 0, len(resp_pdu) + 1, unit) + resp_pdu
            conn.send(resp)
            log("req %s  fc=%s -> %s" % (addr, hex(fc), resp_pdu.hex()))
    except Exception as e:
        log("连接异常 %s: %s" % (addr, e))
    finally:
        conn.close()


def _dispatch(fc, pdu):
    """按功能码处理请求,返回响应 PDU(功能码 + 数据)。"""
    if fc == 0x03:  # 读保持寄存器
        if len(pdu) < 5:
            return bytes([0x83, 0x03])
        start, qty = struct.unpack(">HH", pdu[1:5])
        if qty < 1 or qty > 125 or start < 0 or start + qty > REG_COUNT:
            return bytes([0x83, 0x02])           # 非法数据地址
        data = b"".join(struct.pack(">H", REGS[start + i]) for i in range(qty))
        return bytes([0x03, qty * 2]) + data
    if fc == 0x06:  # 写单个寄存器
        if len(pdu) < 5:
            return bytes([0x86, 0x03])
        addr, val = struct.unpack(">HH", pdu[1:5])
        if addr < 0 or addr >= REG_COUNT:
            return bytes([0x86, 0x02])
        REGS[addr] = val
        log("写入寄存器 0x%04X = %d" % (addr, val))
        return pdu                                 # 回显
    return bytes([fc | 0x80, 0x01])              # 非法功能码


def simulate():
    """后台线程:模拟温湿度缓慢变化,让数据看起来"活"的。"""
    while True:
        time.sleep(5)
        REGS[0] = max(180, min(380, REGS[0] + random.randint(-20, 20)))  # 18.0~38.0°C
        REGS[1] = max(300, min(800, REGS[1] + random.randint(-30, 30)))  # 30.0~80.0%


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main(port=5502):
    threading.Thread(target=simulate, daemon=True).start()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(8)
    print("=" * 60)
    print("Modbus-TCP 从站已启动 0.0.0.0:%d" % port)
    print("本机 IP: %s   <- 填到 ESP32 配网页「Modbus 服务器地址」" % local_ip())
    print("端口: %d   单元ID: 1" % port)
    print("寄存器: 0x0000=温度(×10) 0x0001=湿度(×10) 0x0007=继电器(低8位)")
    print("=" * 60)
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            continue
        threading.Thread(target=handle_connection, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    import sys
    _port = int(sys.argv[1]) if len(sys.argv) > 1 else 5502
    main(_port)
