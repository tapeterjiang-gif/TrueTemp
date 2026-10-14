# TrueTemp

首次使用请阅读 [命令行启动指南](GUIDE.md)。

当前实现：在本机 macOS 上扫描并选择 Sensirion SHT4x Smart Gadget（REF）与 Qingping Monitor Lite2（DUT v3），同时解析广播并采集到文件。支持数据集构建、温度与水汽压残差 LightGBM 训练、独立考试和离线预测。

## 运行

```bash
uv sync --python 3.12
source .venv/bin/activate
truetemp scan
truetemp device show
truetemp collect
```

扫描默认 30 秒，结束后依次输入编号选择 REF 和 DUT。也可直接指定：

```bash
truetemp device set reference REF_UUID
truetemp device set dut DUT_UUID
truetemp scan --no-interactive
truetemp collect --output data/ref01 --note "参考表测试"
```

采集开始后持续运行，Ctrl+C 保存并停止。设备选择写入当前目录 `truetemp.toml`，命令均支持 `--help`；扫描、采集、设备 set/show 支持 `--config`。macOS 设备标识是本机 CoreBluetooth UUID，不是 MAC 地址。

```toml
[devices]
reference = "REF_UUID"
dut = "DUT_UUID"

[collection]
interval = "5s"
```

## 数据

每次采集创建独立目录，已有目录不覆盖：

- `raw.jsonl`：所选 REF 的每次厂商载荷回调、UTC 接收时间、单调时钟、RSSI、解析结果或错误。
- `reference.csv`：每 5 秒区间最新的有效记录，包含接收时间、原始整数与工程量（°C、%RH）。停止时保存最后未满区间的数据。
- `session.json`：设备标识、间隔、备注、起止时间、数量与完成状态。

无新回调时不填充旧值；相同读数的新回调可再次记录。协议没有测量序号，无法据此断言是一次新测量。保存的是 macOS 接口提供的厂商载荷，不是完整空口抓包。

## REF 协议

依据 [Sensirion 官方广播字段定义](https://github.com/Sensirion/ble-services/blob/main/sensirion-ble/src/resources/ble-ad-sample-types.yml) 和 [协议说明](https://sensirion.github.io/ble-services/)：

- Company ID：`0x06D5`，Bleak 将其作为字典键，不包含在下面的 8 字节载荷中。
- 字节 0：广告类型 `0x00`；字节 1：样本类型 `0x06`。
- 字节 2～3：设备短 ID；4～5：温度原始值；6～7：湿度原始值，均为小端 `uint16`。
- `T = -45 + 175 × raw_t / 65535`。
- `RH = -6 + 125 × raw_h / 65535`。

仅将公司 ID、载荷类型及长度匹配且读数有效的设备列为 REF 候选。该协议也用于部分其他 SHT4x 设备，不能仅凭该载荷确认具体产品型号；请选择你的 Smart Gadget。无效湿度保留在原始日志并记录错误，不静默裁剪。

## 蓝牙与测试

开启 Smart Gadget 广播和电脑蓝牙。首次运行可能需要允许终端应用访问蓝牙；若已拒绝，在“系统设置 → 隐私与安全性 → 蓝牙”中开启对应应用权限。若设备被其他客户端连接，可能停止广播，先断开该连接。

```bash
uv run pytest
```

测试使用人工构造协议向量和模拟扫描器，验证解析、负温度、无效报文、配置、采样及停止保存；真实设备可用性需在本机扫描验证。

## 导出 CSV

```bash
truetemp export data/ref01
truetemp export data/ref01 --output exports/ref01.csv
```

输入为采集会话目录；默认输出到会话目录旁的 `data/ref01.csv`。导出已采样的 `reference.csv`，保留采集字段（不含旧版 `monotonic_seconds`）、接收时间；温度和湿度统一保留 1 位小数，温度为 °C、湿度为 %RH，不重新采样或导出逐包原始日志。使用 UTF-8 BOM 编码，便于表格软件读取。命令显示导出记录数及路径；已有文件不覆盖。建议 Ctrl+C 停止采集后导出完整会话，无有效样本时导出仅含表头的文件。无需设备配置或蓝牙连接。

`reference.csv` 不包含 `monotonic_seconds`，以 `received_at` 表示接收时间。单调时钟仅保留于 `raw.jsonl` 用于诊断；导出旧会话时也会自动移除该列，原会话文件不修改。

`received_at` 保留到秒，格式示例：`2026-09-06T12:00:00+00:00`（UTC，北京时间加 8 小时）。小数秒直接省略，不四舍五入；旧会话导出时也采用此格式。

## DUT 功能

`collect` 在已配置 DUT 时同时采集两台设备；未配置 DUT 时继续仅采集 REF。使用同一扫描器，每 5 秒分别保存最新有效记录，不填充旧值、不强行进行时间配对。

DUT 新增 `dut.csv`、`dut_raw.jsonl` 和 `dut_session.json`；REF 文件保持原有名称，主 `session.json` 记录本次 DUT 标识。双方记录接收时间到秒，CSV 不含单调时钟，温湿度保留 1 位小数。DUT 的 `raw_temperature` 和 `raw_humidity` 是未校正的工程量，不是广播整数编码。原始整数可从 `dut_raw.jsonl` 的载荷还原。全部字段见 [DUT 协议](DUT_PROTOCOL.md)。

```bash
uv run truetemp export data/ref01 --device dut
uv run truetemp export data/ref01 --device dut --output exports/dut01.csv
```

DUT 默认导出到 `data/ref01_dut.csv`。不指定 `--device` 时仍导出 REF。

数据集、LightGBM 物理校正、独立考试与预测见 [系统设计](SYSTEM_DESIGN.md) 和 [操作指南](GUIDE.md)。

## 本机标定

当前 Mac 已安装 LightGBM 的 `libomp` 运行依赖。新环境先执行 `brew install libomp`、`uv sync --python 3.12`。

```bash
# 先按 examples/experiment.toml 填写真实独立实验，路径相对清单
uv run truetemp dataset build experiment.toml --output datasets/dut01
uv run truetemp train datasets/dut01 --output models/dut01
uv run truetemp evaluate datasets/dut01 --model models/dut01 --output reports/exam01 --exam
uv run truetemp predict data/new01/dut.csv --model models/dut01 --output predictions/new01.csv
```

训练仅使用 train/validation，考试只使用 test。湿度先基于校正温度与水汽压关系补偿，再使用水汽压残差模型校正。样本不足或验证集更支持常数基线时会明确提示。

湿度范围与工况覆盖未确认前，考试结论为待验收或误差未通过，不代表已达到 DUT 绝对精度目标。完整流程和配置见 [GUIDE](GUIDE.md)。
