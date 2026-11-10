# DUT 遥测协议 v3

来源：用户提供的固件布局（2026-09-11），已收到 Qingping Monitor Lite2 的真实广播。

Manufacturer Specific Data 共 23 字节，全部小端，其中 0–1 为公司 ID（0xDFDF）。Bleak 会把公司 ID 从载荷值中拆出，因此解析函数 `decode_dut` 接收的是剩余 21 字节内容（从 version 开始），公司 ID 的匹配在扫描/采集环节依据 manufacturer_data 键完成。

| 完整载荷偏移 | 字段 | 类型 / 单位 |
| --- | --- | --- |
| 0–1 | company_id | `0xDFDF` |
| 2 | version | uint8，`3` |
| 3 | flags | uint8，当前为 0；bit0 保留绑定标志 |
| 4–5 | raw_temperature | int16，0.1°C |
| 6–7 | raw_humidity | int16，0.1%RH |
| 8–9 | temperature_soc | int16，0.1°C，P4 SoC |
| 10–11 | temperature_co2 | int16，0.1°C，SCD4x 内部温度 |
| 12–13 | time_uptime | uint16 秒，C5 本次启动，最大 7200 |
| 14 | screen_brightness | uint8，0～255 |
| 15–16 | screen_on_duration_s | uint16 秒，最大 3600 |
| 17 | cpu_load_10s | uint8，P4 过去 10 秒平均负载，0～100% |
| 18 | wifi_load_10s | uint8，C5 过去 10 秒平均负载，0～100% |
| 19–20 | power_charge_ma | int16 mA，正值表示充电 |
| 21–22 | c5_soc_temp | int16，0.1°C，C5 SoC，v3 末尾追加字段 |

CSV 包含 `received_at`、`device_id`、`rssi`，以及表中 version 至 c5_soc_temp 的字段。五个温度/湿度字段（raw_temperature、raw_humidity、temperature_soc、temperature_co2、c5_soc_temp）已除以 10，以 1 位小数保存，其余为整数。字段名里的 raw 表示未校正，不表示 CSV 仍保存十分之一单位的整数。

SoC 温度（P4 的 temperature_soc 与 C5 的 c5_soc_temp）数据源暂无时为 0。其他 telemetry 镜像尚未到达时也可为 0；协议没有有效性位，程序保留原值，不把所有 0 自动认作缺失或正常测量。flags 原样保存，不据此过滤设备。

拒绝长度或版本不符、湿度超出 0～100%RH、计时超过上限、负载超过 100% 的报文，并在所选设备原始日志中记录原因。company_id 不符的设备根本不会被当作 DUT 接收。负温度和负电流允许通过。

## Snow2S 遥测 v4

厂商 ID 同为 `0xDFDF`，Bleak 去掉厂商 ID 后为 35 字节。所有多字节字段小端。

| 载荷偏移（不含厂商 ID） | 字段 | 类型 / 单位 |
| --- | --- | --- |
| 0–1 | version, flags | 各 uint8。version 为 4。flags bit7 表示光感有效 |
| 2–3 | 广播序号 | uint16 |
| 4–11 | 原始温湿度、SoC 温度、CO₂ 模块温度 | 各 int16，十分位 |
| 12–13 | 设备运行时长 | uint16 秒，最大 7200 |
| 14–15 | screen_brightness | uint16，原始背光 0～1000 |
| 16–17 | 持续亮屏时长 | uint16 秒，最大 7200 |
| 18–19 | CPU 负载 | uint16，十分位百分比 |
| 20–21 | ambient_lux | uint16，lux |
| 22 | Wi-Fi RSSI | int8，dBm |
| 23–24 | wifi_tx_bytes | uint16，Byte/s |
| 25–28 | 电池侧充电电流与电压 | 各 uint16，mA / mV |
| 29–34 | BLE MAC | 6 字节 |

`./collect` 与包内采集命令均接收 v3 和 v4。v4 采集记录保留协议版本和原始背光，
无效 lux 及 v4 没有提供的 Wi-Fi 负载、C5 温度留空。发送速率单独记录。
Rockchip Linux 的 v4 训练及推理入口见 [Linux 集成](README.md#snow2s-v4)。
`src/truetemp` 的既有校准模型入口使用 v3 数据。
