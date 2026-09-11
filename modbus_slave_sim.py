# -*- coding: utf-8 -*-
"""
Modbus TCP 从站模拟器（仅用于本地测试）
==============================================
当没有真实 Modbus 从站时（地址见 config.MODBUS_HOST），可本地启动一个模拟从站，
便于测试 modbus_collector.py 的读取/写入功能。

用法：
  python modbus_slave_sim.py                 # 监听 0.0.0.0:5502
  python modbus_slave_sim.py --port 5502     # 自定义端口

说明：
  - 寄存器 0x0000~0x0009 初始化一组固定值；
  - 可通过采集器修改寄存器值（例如 python modbus_collector.py --write 0x0000 100），
    写入会实时生效，随后采集器即可读到新值。
"""
import argparse
import asyncio
import logging

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import SimDevice
from pymodbus.simulator.simdata import DataType, SimData

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("slave-sim")


async def run_server(port):
    reg = SimData(
        config.REGISTER_START,
        count=config.REGISTER_TOTAL,
        values=[1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000],
        datatype=DataType.REGISTERS,
    )
    device = SimDevice(id=config.MODBUS_UNIT, simdata=reg)
    server = ModbusTcpServer(device, address=("0.0.0.0", port))

    log.info("Modbus TCP 从站模拟器已启动，监听 0.0.0.0:%s，unit=%s，寄存器 0x0000-0x0009",
             port, config.MODBUS_UNIT)
    await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Modbus TCP 从站模拟器")
    parser.add_argument("--port", type=int, default=config.MODBUS_PORT, help="监听端口")
    args = parser.parse_args()
    try:
        asyncio.run(run_server(args.port))
    except KeyboardInterrupt:
        log.info("已停止")


if __name__ == "__main__":
    main()
