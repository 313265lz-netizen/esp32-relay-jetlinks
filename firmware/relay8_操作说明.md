# 8 路继电器 + Modbus 网关 JetLinks 固件使用说明

> 固件:`relay8_mqtt.py`(ESP32-C3 / MicroPython v1.24,单文件)
> 一款基于 ESP32-C3 的 8 路继电器 + Modbus-TCP 网关固件:周期采集从站寄存器、
> 转 JSON 上报 JetLinks,分时调度保证采集不阻塞继电器控制。

## 1. 硬件引脚(已按实物确认)

| 项 | 引脚 |
| --- | --- |
| 继电器 1~4(真实,低电平吸合:0=开 1=关) | GPIO **3 / 4 / 5 / 7** |
| 继电器 5~8(虚拟通道,无物理输出,仅记录/上报) | 无 |
| SW1~SW4 按键(短按切换继电器1~4;SW1 长按 5 秒进配网,上拉按下为低) | GPIO **10 / 9 / 6 / 8** |

> 平台需要上报 8 路继电器状态,实际板子只有 4 路物理继电器,所以固件里
> `switch_1~switch_4` 接真实继电器,`switch_5~switch_8` 作为虚拟通道(状态仍会
> 上报、可被平台下发"开/关"并记录,只是没有物理动作)。
> 若以后换成 8 路真实继电器板,只需把 `relay8_mqtt.py` 顶部
> `RELAY_PINS = [3, 4, 5, 7, None, None, None, None]` 的 4 个 `None` 改成真实 GPIO 即可。

## 2. 刷固件

1. 刷 MicroPython 固件(芯片 ESP32-C3、4MB flash):
   `esptool --port <串口> write_flash -z 0x0 LOLIN_C3_MINI-20241025-v1.24.0.bin`
2. 用 Thonny 打开 `relay8_mqtt.py` → **文件 → 另存为 → MicroPython 设备 → 命名 `main.py`**
3. 按 RST/重新上电,开机自动运行。

## 3. 配网模式(首次使用 / 长按 SW1 / WiFi 连不上)

1. **长按 SW1 约 5 秒**(或首次上电没有 WiFi 配置时自动进入),板子放出热点
   `Relay8-Setup`(无密码),继电器全部关闭。
2. 手机连上该热点,浏览器打开 `http://192.168.4.1`。
3. 填写表单:

   | 字段 | 说明 / 默认 |
   | --- | --- |
   | WiFi 名称/密码 | 你的 2.4GHz WiFi(设备联网用) |
   | MQTT 服务器地址 | `<你的 MQTT 服务器地址>`(JetLinks MQTT 网关) |
   | MQTT 端口 | `9783` |
   | MQTT 用户名/密码 | 你的 MQTT 账号(可留空) |
   | 产品 ID(productId) | `<你的产品 ID>`(需与平台产品一致) |
   | 设备 ID | **留空 = 自动用设备 MAC** |
   | Modbus 服务器地址 | 电脑端从站 IP(**留空 = 不采集**) |
   | Modbus 端口 | `5502` |
   | Modbus 单元 ID | `1` |
   | Modbus 采集周期 | `5` 秒(可 5~120) |
   | Modbus 寄存器配置 | JSON,见下方「Modbus 网关」一节 |

4. 点「保存并重启」→ 设备重启 → 连 WiFi → 连 MQTT → 上报上线 + 全量状态。
   串口会打印 `WiFi connected: ...`、`MQTT connected: ...`,若填了 Modbus 服务器还会打印
   `Modbus 采集: host=...`。

> 配置持久化在板子 `config.json`,断电重启直接读它联网,无需再配网。

## 4. JetLinks 物模型 MQTT 协议

主题(前导斜杠):

```text
/{productId}/{deviceId}/properties/report         设备上报属性
/{productId}/{deviceId}/properties/read           平台下发:读取
/{productId}/{deviceId}/properties/read/reply     设备回复
/{productId}/{deviceId}/properties/write          平台下发:修改(开/关)
/{productId}/{deviceId}/properties/write/reply    设备回复
/{productId}/{deviceId}/function/invoke           平台下发:功能调用(开/关)
/{productId}/{deviceId}/function/invoke/reply     设备回复
/{productId}/{deviceId}/online                    设备上线
```

上报 payload 示例:

```json
{"messageId":"a1b2...","properties":{"switch_1":0,"switch_2":1,...,"switch_8":0}}
```

下发(平台 → 设备,发布到 `.../properties/write`):

```json
{"messageId":"abc","properties":{"switch_3":1}}
```

设备执行后回复 `.../properties/write/reply`,并立即补报一次真实状态,保证「状态同步一致」。

## 5. 用 MQTTX 自测(不依赖平台,快速验证)

MQTTX 新建连接:Host 填你的 MQTT 服务器地址、Port `9783`,用户名/密码为你的 MQTT 账号。

1. 订阅 `/{productId}/{deviceId}/#`,会看到:
   - `.../online`(上线)
   - `.../properties/report`(全量状态,之后每约 30 秒一次心跳)
2. 发布到 `/{productId}/{deviceId}/properties/write`:
   ```json
   {"messageId":"t1","properties":{"switch_1":1}}
   ```
   应看到继电器 1 吸合,同时 `.../properties/write/reply` 和 `.../properties/report` 出现。
3. 发布 `{"messageId":"t2","properties":{"switch_1":0}}` 关闭。

> 把 `{productId}` 换成你的产品 ID,`{deviceId}` 换成设备 MAC(串口打印,或配网页里能看到)。

### 5.1 用 JetLinks 平台「功能调用」点按钮控制

想在平台页面点按钮(而不是 MQTTX 发报文)控制继电器,用「功能定义」:

1. JetLinks → 设备管理 → 你的产品 → 物模型 → **功能定义** → 添加功能:

   | 字段 | 填什么 |
   | --- | --- |
   | 标识 | `setSwitch` |
   | 名称 | 设置继电器 |
   | 是否异步 | 否(同步) |
   | 输入参数 | 见下 |
   | 输出参数 | 空 |

   输入参数两个:

   | 参数名 | 类型 | 说明 |
   | --- | --- | --- |
   | `channel` | 整数 | 第几路(1~8) |
   | `state` | 布尔 | 开=true,关=false |

2. 保存后打开设备 7ce8b1c1a7a0 详情页 → **功能** → `setSwitch` → 填
   `channel=1`、`state=开` → 点**调用**,继电器 1 吸合。

   固件收到 `/function/invoke` 后执行并回复 `/function/invoke/reply`,页面显示调用成功。

## 6. Modbus 网关

固件把 ESP32 变成「Modbus-TCP 主机(网关)」:周期轮询从站寄存器 → 转 JSON → 通过
MQTT 上报到 JetLinks。调试阶段不用接任何 RS485 硬件,电脑跑一个 Python 脚本模拟
从站即可。

### 6.1 电脑端从站(模拟"另一台设备")

```bash
python modbus_tcp_slave.py
```

启动后屏幕打印本机 IP(例如 `192.168.x.x`),并提供一个 Modbus-TCP 从站(端口 5502):

| 寄存器 | 含义 | 说明 |
| --- | --- | --- |
| `0x0000` | 温度 | 值 ×10,如 `256` = 25.6°C,每 5 秒缓慢漂移 |
| `0x0001` | 湿度 | 值 ×10,如 `500` = 50.0% |
| `0x0007` | 8 路继电器 | 低 8 位(bit0~7),可读可写 |

支持功能码 `0x03`(读保持寄存器)、`0x06`(写单寄存器)。

### 6.2 网关采集与上报

- 配网页里把「Modbus 服务器地址」填成上面那个电脑 IP,端口 5502,单元 ID 1。
- 默认寄存器配置:

  ```json
  [{"addr":0,"key":"temperature","scale":0.1},{"addr":1,"key":"humidity","scale":0.1}]
  ```

  `key` 是上报到 JetLinks 的属性名,`scale` 是缩放(×0.1 把 256 变成 25.6)。

- 每 5 秒(可配)采集一次,读到就发到 `.../properties/report`:

  ```json
  {"messageId":"...","properties":{"temperature":25.6,"humidity":50.0}}
  ```

### 6.3 分时调度(不阻塞继电器)

ESP32 是单线程,固件用「分时调度」:采集按周期触发,每次读寄存器只等最多 1.5 秒
(socket 短超时),到点没响应就跳过,绝不死等。因此 Modbus 采集卡住/超时不会影响
继电器控制与 MQTT 消息监听。

### 6.4 关键日志

调试时串口会打印关键点,便于快速定位:

```text
Modbus 采集: host=192.168.x.x:5502 unit=1 周期=5s regs=[...]
Modbus 采集 -> {'temperature': 25.6, 'humidity': 50.0}
Modbus 采集失败(从站不可达/超时)
```

## 7. 主要特性

- 配网方便：长按 SW1 或首次上电进入 AP 热点，网页填一次就存到 config.json，断电重启自动重连
- 继电器控制：订阅 properties/write 驱动 GPIO3/4/5/7，执行后回 write/reply 并补报真实状态，保证两端一致
- 状态上报：switch_1~switch_8 全量上报 + 30 秒心跳，断线自动重连
- Modbus 网关：按周期轮询从站寄存器转 JSON 上报，寄存器配置可网页下发并持久化
- 不阻塞：分时调度 + socket 短超时，采集超时/失败不影响继电器和 MQTT 监听
- 好排查：串口打印采集结果、失败原因等关键日志

## 8. 注意事项

- **设备 ID 用 MAC 时**:需在 JetLinks 平台对应产品下创建一个 deviceId 等于该 MAC
  的设备,主题才能对上。若平台已有设备,在配网页把「设备 ID」填成该设备 ID 即可。
- ESP32-C3 只支持 2.4GHz WiFi,5GHz 连不上。
- WiFi 连不上时上电 30 秒后会自动回到配网热点,不会失联。
- 固件无 LWT(umqtt.simple 不支持遗嘱),平台以「超过约 90 秒没心跳」判离线;固件每
  30 秒心跳一次,正常不会误判。

## 9. 离线自测(不接硬件)

```bash
python test_relay8_mock.py
```

会 mock 硬件跑一遍配置读写、MAC、状态、下发命令、URL 解码、Modbus 寄存器解析与采集等
54 项断言,全通过即可。
