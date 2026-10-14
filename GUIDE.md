# TrueTemp 命令行启动指南

本指南适用于当前 Mac（macOS / Apple Silicon）。当前版本支持 Sensirion SHT4x Smart Gadget 标准表（REF）与 DUT v3 的扫描、选择、采集和 CSV 导出。

## 1. 打开终端并进入项目

打开 macOS 的“终端”应用，复制执行：

```bash
cd /Users/peterjiang/Documents/Codex/TrueTemp
```

后续命令都在此目录执行，设备配置默认保存到该目录的 `truetemp.toml`。

## 2. 首次准备运行环境

当前电脑已安装 `uv`，用于管理 Python 环境和项目依赖。首次使用或项目更新后执行：

```bash
uv sync --python 3.12
```

确认程序可运行：

```bash
uv run truetemp --help
```

本指南统一使用 `uv run truetemp` 启动，无需手动激活虚拟环境。

## 3. 扫描并选择标准表和 DUT

开启 Smart Gadget 的蓝牙广播，将设备放在电脑附近，然后执行：

```bash
uv run truetemp scan
```

首次扫描时，如果 macOS 询问是否允许终端使用蓝牙，请选择允许。

默认扫描 30 秒，显示发现的 REF/DUT、信号强度和温湿度。扫描结束后，根据编号分别选择标准表和 DUT：

```text
选择标准表 [编号 / Enter 跳过 / q 退出]：1
选择DUT [编号 / Enter 跳过 / q 退出]：2
```

输入的 `1` 和 `2` 只是示例，请以实际扫描列表中的编号为准。两个角色选择完成后配置一次保存，不会自动开始采集。

- 已配置标准表时，直接按 Enter 保留原选择。
- 输入 `q` 退出，不修改配置。
- 扫描过程中按 Ctrl+C 可提前结束扫描并进入选择；选择过程中按 Ctrl+C 退出。

查看已保存的设备：

```bash
uv run truetemp device show
```

如果已有扫描得到的设备 UUID，也可以直接设置。将下面的 `REF_UUID`、`DUT_UUID` 替换为实际标识：

```bash
uv run truetemp device set reference REF_UUID
uv run truetemp device set dut DUT_UUID
```

macOS 使用本机扫描得到的 UUID 标识设备，不是设备的 MAC 地址。

## 4. 开始采集

```bash
uv run truetemp collect
```

配置 DUT 后会同时采集 REF 和 DUT；只有 REF 配置时仍可单表采集。程序立即持续采集，并显示接收状态、样本数和最新温湿度。默认每 5 秒记录区间内最新的有效数据；没有新广播时留缺，不复制旧数据。

默认输出到 `data/<时间戳>`。也可以指定便于记忆的目录和备注：

```bash
uv run truetemp collect --output data/ref01 --note "标准表测试"
```

已有目录不会覆盖。重复实验时请换一个新目录，例如 `data/ref02`。

## 5. 停止采集

在正在采集的终端中，按住 Control 键并按 C（**Ctrl+C**）。

程序会保存剩余数据、显示保存路径并退出。建议用此方式结束采集，再关闭终端窗口。

会话目录包含：

| 文件 | 内容 |
| --- | --- |
| `reference.csv` | 按采样间隔记录的温度、湿度、接收时间等数据 |
| `raw.jsonl` | 收到的 REF 厂商广播载荷及解析结果 |
| `session.json` | 设备、采集间隔、备注、起止时间和 REF 统计信息 |
| `dut.csv` | DUT v3 参数的采样数据（配置 DUT 时生成） |
| `dut_raw.jsonl` | DUT 原始广播和解析结果 |
| `dut_session.json` | DUT 采集状态和统计信息 |

## 6. 导出 CSV

将已停止的采集会话导出为单独的 CSV 文件：

```bash
uv run truetemp export data/ref01
```

默认导出 REF，生成 `data/ref01.csv`。导出 DUT 使用：

```bash
uv run truetemp export data/ref01 --device dut
```

生成 `data/ref01_dut.csv`。指定导出位置：

```bash
uv run truetemp export data/ref01 --output exports/ref01.csv
```

请将 `data/ref01` 替换为你的实际采集目录。导出保留采样数据字段，温度和湿度统一保留 1 位小数（旧版 `monotonic_seconds` 列自动省略），温度单位为 °C，湿度单位为 %RH；已有输出文件不会覆盖。

## 7. 下次使用

设备已保存后，每次只需打开终端并执行：

```bash
cd /Users/peterjiang/Documents/Codex/TrueTemp
uv run truetemp collect
```

需要更换标准表时，再运行扫描并重新选择。

## 8. 校准流程

以下命令已实现，可在本机运行。完整参数参考见 [CLI 文档](CLI.md)。

### 8.1 准备独立实验数据

先使用已实现的采集命令分别记录训练、验证和考试实验，每次完成后按 Ctrl+C：

```bash
uv run truetemp collect --output data/train01 --note "训练实验"
# 按 Ctrl+C，保存后再进行下一次独立实验
uv run truetemp collect --output data/valid01 --note "验证实验"
# 按 Ctrl+C
uv run truetemp collect --output data/test01 --note "独立考试实验"
# 按 Ctrl+C
```

每次实验都需要 REF 和 DUT 的有效数据，并覆盖计划中的温湿度与设备状态。三次采集只是操作示例，不意味着三份文件就足以覆盖目标范围。不要将一次连续实验拆成三个文件冒充独立数据。

保留整个采集目录及原始日志，不要只留下导出的 CSV。

### 8.2 编写实验清单

在项目目录新建 `experiment.toml`，填写实际会话路径：

```toml
[[sessions]]
path = "data/train01"
experiment_id = "experiment-01"
condition_id = "25C-screen-off"
split = "train"

[[sessions]]
path = "data/valid01"
experiment_id = "experiment-02"
condition_id = "25C-screen-on"
split = "validation"

[[sessions]]
path = "data/test01"
experiment_id = "experiment-03"
condition_id = "25C-independent-repeat"
split = "test"

[alignment]
max_gap = "2s"
```

- `path` 相对清单文件所在目录；工况名称仅为示例，请填写实际条件。
- `experiment_id` 表示独立实验，同一次实验的所有会话使用相同 ID 和 split。
- `train` 用于拟合，`validation` 用于选参数和早停，`test` 只在模型确定后考试。
- `max_gap` 是 REF/DUT 时间配对容差，与 5 秒采集间隔不同。不要为了增加配对数任意放宽。

### 8.3 构建数据集与训练

执行：

```bash
uv run truetemp dataset build experiment.toml --output datasets/dut01
uv run truetemp train datasets/dut01 --output models/dut01
```

先检查数据质量报告中的配对数量、剔除原因和工况覆盖，再训练。模型目录包含温度修正、水汽压残差模型及元数据。样本不足、特征恒定或验证集更支持基线时，使用 JSON 保存常数残差，不生成对应的 `.txt`。

### 8.4 独立考试

```bash
uv run truetemp evaluate datasets/dut01 --model models/dut01 --output reports/exam01 --exam
```

打开 `reports/exam01/report.md` 查看结论，`predictions.csv` 查看逐条结果，`metrics.json` 保存全精度指标。

考试比较原始、湿度物理补偿和最终校正的结果。温度参考误差门槛为 ±0.3°C，湿度为 ±3%RH，同时检查工况覆盖和质量失败。覆盖未确定或不足时不会宣布全范围达标。

带 `--exam` 时，通过返回 `0`，未通过或待验收返回 `3`；在终端可紧接着执行 `echo $?` 查看。省略 `--exam` 时只评分生成报告，不以精度未通过作为命令失败。

考试后根据结果调过模型，就应换新的独立考试数据。只有模型固定且考试数据未参与开发，才能将其作为最终验证。相对 REF 达标不等同于 DUT 绝对精度达标。

### 8.5 校正后续 DUT 数据

```bash
uv run truetemp predict data/new01/dut.csv --model models/dut01 --output predictions/new01.csv
```

输入使用当前 DUT CSV 的字段与单位，预测无需参考表数据。输出包含校正温度、物理补偿湿度、最终湿度和质量标记；原始数据保留不变。

请将示例目录换为真实路径，已有输出不会覆盖。运行 `uv run truetemp --help` 查看可用功能。

## 常见问题

**提示 `command not found: truetemp`**

使用完整启动方式 `uv run truetemp`，不要省略 `uv run`。

**提示 `command not found: uv`**

当前电脑的 `uv` 位于 `/opt/homebrew/bin/uv`。可使用完整路径，例如：

```bash
/opt/homebrew/bin/uv run truetemp --help
```

**扫描不到标准表**

确认电脑蓝牙和 Smart Gadget 广播已开启。若手机 App 正连接设备，先断开连接；将设备靠近电脑，并尝试延长扫描：

```bash
uv run truetemp scan --duration 60s
```

**提示蓝牙权限错误**

在“系统设置 → 隐私与安全性 → 蓝牙”中，允许实际运行程序的应用使用蓝牙；使用 macOS 终端时对应“终端”。

**采集显示等待广播、广播中断或样本增长较慢**

5 秒是主机记录间隔，不保证每 5 秒都能收到广播。检查设备广播状态和距离；程序不会用旧值填满缺失区间。

**查看命令帮助**

```bash
uv run truetemp --help
uv run truetemp scan --help
uv run truetemp collect --help
uv run truetemp export --help
```

`received_at` 保留到秒，格式示例：`2026-09-06T12:00:00+00:00`（UTC，北京时间加 8 小时）。小数秒直接省略，不四舍五入；旧会话导出时也采用此格式。

采集期间，DUT 输出除温湿度外，还显示 SoC/CO₂ 温度、运行时间、屏幕亮度、持续亮屏时间、P4/C5 近 10 秒平均负载及充电电流（正值表示充电）。附最近有效读数的接收年龄，便于判断数据是否已过期。零值按协议原样显示，可能表示镜像数据尚未就绪。

## 训练环境与配置样例

首次在当前 Mac 安装模型依赖：

```bash
brew install libomp
uv sync --python 3.12
uv run truetemp train --help
```

当前电脑已安装 `libomp`。可参考 [experiment.toml](examples/experiment.toml) 和 [training.toml](examples/training.toml)；完整配置含义见 [CLI 第 10 节](CLI.md#10-实验标记覆盖与训练配置)。

采集结束并确认双方有数据后再构建数据集。仍标记为运行中的会话会被拒绝，零有效配对不会生成训练集。`quality.json` 可查看无效包、排除数、未配对数、复用和重启统计。

请按实际状态设置每个会话的 `phase`。未标记稳态、未确定湿度范围或工况覆盖不足，都不能用于声明全范围通过。通过 `[coverage]` 声明的门槛随模型冻结；需修改时重新训练并安排独立考试。
