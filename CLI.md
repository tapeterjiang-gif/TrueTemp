# TrueTemp CLI

当前阶段实现 REF 和 DUT 的扫描、选择及广播采集。运行环境为本机 macOS 26.6.2 / arm64，Python 3.12。已支持数据集构建、LightGBM 训练、独立考试和离线预测。

## 命令状态

| 命令 | 状态 | 用途 |
| --- | --- | --- |
| `scan`、`device set/show` | 已实现 | 扫描、选择及配置 REF/DUT |
| `collect`、`export` | 已实现 | 采集和导出 CSV |
| `dataset build` | 已实现 | 构建含明确划分的数据集 |
| `train` | 已实现 | 训练两个 LightGBM 模型 |
| `evaluate` | 已实现 | 独立数据集评分与考试 |
| `predict` | 已实现 | 对 DUT 文件进行校正 |

当前 `--help` 仅列出已实现命令。以下命令参考使用 `truetemp`；未激活虚拟环境时，请加 `uv run`。

## 1. 扫描并选择标准表和 DUT

```bash
truetemp scan
truetemp scan --duration 20s
```

默认扫描 30 秒，终端中原位更新列表，序号保持稳定。显示 Sensirion REF 和 0xDFDF v3 DUT 广播；通过公司 ID 与协议识别角色。

```text
TrueTemp · REF / DUT 扫描
编号  设备名称      类型             信号      温度      湿度       当前设置
1     SHT4x Gadget SHT4x REF 候选   -48 dBm   25.3°C   48.2%RH   —
2     Qingping     DUT v3 候选      -44 dBm   26.6°C   32.8%RH   —

扫描中 · 6/30 秒 · Ctrl+C 进入选择
```

扫描完成后显示完整设备标识，再提示：

```text
选择标准表 [编号 / Enter 跳过 / q 退出]：1
选择DUT [编号 / Enter 跳过 / q 退出]：2
已保存到 /项目目录/truetemp.toml
标准表：SHT4x Gadget  <REF_UUID>
运行 truetemp collect 开始采集，Ctrl+C 保存并停止。
```

- 已配置时 Enter 保留当前选择，未配置时跳过；选择阶段 Ctrl+C 或 `q` 不保存本次两个角色的选择；两步结束后一次保存。
- 只有成功解析的 SHT4x 广播可选为 REF；无效设备显示原因。
- 当前设备本轮未发现时保留配置并提示，不自动替换。
- 超过 10 秒未更新的扫描读数标记过期。
- 扫描失败给出错误；未发现设备时提示检查广播和蓝牙状态。
- 设置完成后不自动采集。
- `--no-interactive` 仅显示结果；标准输入或输出不是终端时自动使用此模式。
- `--config PATH` 指定配置文件，默认当前目录的 `truetemp.toml`。

示例名称和数值仅用于展示。协议识别表明设备兼容 SHT4x 广播格式，不保证它一定是 Smart Gadget；用户通过设备标识选择实际参考表。

## 2. 直接设置或查看设备

```bash
truetemp device set reference REF_UUID
truetemp device set dut DUT_UUID
truetemp device show
```

设置后保存到配置文件，保留其他配置。文件不存在时创建。这两个命令均支持 `--config PATH`。macOS 使用扫描得到的本机 UUID，不使用 MAC 地址。

## 3. 采集

```bash
truetemp collect
truetemp collect --output data/ref01 --note "参考表测试"
```

- 读取已设置的 REF 和 DUT，立即持续采集，Ctrl+C 保存并停止；无采集时长参数。
- 无 REF 配置时提示先选择设备；未设置 DUT 时继续仅采集 REF。
- 默认间隔为 5 秒，每个区间记录最新的有效接收数据，无新报文时留缺。
- 保留实际接收时间，不按相同数值去重；REF 协议没有测量序号，不能区分相同数值的重复广播与新测量。
- 终端显示有效样本数、报文数、异常数量、最新温湿度及接收状态。
- `--output` 默认 `data/<时间戳>`；已有目录不覆盖。
- `--note` 为可选备注；`--config PATH` 指定配置文件。

输出 `raw.jsonl`、`reference.csv` 和 `session.json`。停止时保存最后未满 5 秒区间的有效记录。具体字段及协议见 [使用说明](README.md)。

## 4. 配置和退出码

```toml
[devices]
reference = "REF_UUID"
dut = "DUT_UUID"

[collection]
interval = "5s"
```

间隔只控制主机记录节奏，不修改设备广播频率。命令均支持 `--help`，版本使用 `truetemp --version`。

成功或正常停止返回 `0`，参数/配置错误返回 `2`，蓝牙或文件操作失败返回 `1`。

## 5. 当前完整流程

```bash
uv sync --python 3.12
source .venv/bin/activate
truetemp scan
truetemp device show
truetemp collect
# Ctrl+C 保存并停止
```

## 导出 CSV

```bash
truetemp export data/ref01
truetemp export data/ref01 --output exports/ref01.csv
```

输入为采集会话目录；默认输出到会话目录旁的 `data/ref01.csv`。导出已采样的 `reference.csv`，保留采集字段（不含旧版 `monotonic_seconds`）、接收时间；温度和湿度统一保留 1 位小数，温度为 °C、湿度为 %RH，不重新采样或导出逐包原始日志。使用 UTF-8 BOM 编码，便于表格软件读取。命令显示导出记录数及路径；已有文件不覆盖。建议 Ctrl+C 停止采集后导出完整会话，无有效样本时导出仅含表头的文件。无需设备配置或蓝牙连接。

`received_at` 保留到秒，格式示例：`2026-09-06T12:00:00+00:00`（UTC，北京时间加 8 小时）。小数秒直接省略，不四舍五入；旧会话导出时也采用此格式。

DUT 采集文件为 `dut.csv`，原始日志为 `dut_raw.jsonl`，统计为 `dut_session.json`。使用 `truetemp export SESSION --device dut [--output FILE.csv]` 导出 DUT，默认输出 `<会话名>_dut.csv`；默认 `--device reference` 保持旧命令行为。

采集期间，DUT 输出除温湿度外，还显示 SoC/CO₂ 温度、运行时间、屏幕亮度、持续亮屏时间、P4/C5 近 10 秒平均负载及充电电流（正值表示充电）。附最近有效读数的接收年龄，便于判断数据是否已过期。零值按协议原样显示，可能表示镜像数据尚未就绪。

数据集、LightGBM 物理校正、独立考试与预测见 [系统设计](SYSTEM_DESIGN.md) 和 [操作指南](GUIDE.md)。

## 6. 构建数据集

```bash
truetemp dataset build experiment.toml --output datasets/dut01
```

| 参数 | 说明 |
| --- | --- |
| `MANIFEST` | TOML 实验清单，必填；样例见 [GUIDE](GUIDE.md) |
| `--output` | 新数据集目录，必填 |

清单指定会话目录、实验组、工况、`train` / `validation` / `test` 和配对容差（默认 2 秒）。会话路径相对清单文件解析，CLI 输出路径相对当前工作目录。

从 REF/DUT 原始日志重建并配对数据，输出内部全精度 CSV、来源指纹和质量报告。训练需要双方数据；仅 REF 的会话不适用。不能用用户导出的舍入 CSV 替代原始日志。

允许只构建 test 数据集，用于给已有模型考试。一个实验组不能跨集合，重复来源或样本会被拒绝。

## 7. 训练

```bash
truetemp train datasets/dut01 --output models/dut01
truetemp train datasets/dut01 --output models/dut02 --config training.toml
```

| 参数 | 说明 |
| --- | --- |
| `DATASET` | `dataset build` 生成的数据集目录，必填 |
| `--output` | 新模型目录，必填 |
| `--config` | 可选训练配置；省略时使用版本化默认参数，不读取采集配置作为训练参数 |

只读取 train / validation，要求二者有有效数据和独立实验组。输出温度修正模型、水汽压残差模型、特征与物理计算规则、训练来源及验证指标。test 不参与早停、调参或特征选择。

训练配置完整字段见本文末尾；首次使用可省略 `--config`。详细模型策略见 [系统设计](SYSTEM_DESIGN.md)。

## 8. 评分与考试

```bash
# 生成报告，不因精度未达标而返回失败
truetemp evaluate datasets/dut01 --model models/dut01 --output reports/review01

# 正式考试：启用精度与覆盖门槛退出码
truetemp evaluate datasets/dut01 --model models/dut01 --output reports/exam01 --exam
```

| 参数 | 说明 |
| --- | --- |
| `DATASET` | 含独立 test split 的数据集目录，必填 |
| `--model` | 固定模型目录，必填 |
| `--output` | 新报告目录，必填 |
| `--exam` | 启用考试门槛，默认不启用 |

考试只读取 test，不再训练。拒绝与模型训练/验证来源重复的数据。报告比较原始、物理补偿和最终校正，输出 `report.md`、`metrics.json`、`predictions.csv`。

`--exam` 通过要求：范围内有效样本最大绝对误差不超过 0.3°C / 3%RH、质量失败为零，且满足声明的工况覆盖；覆盖未定或不足为“待验收”。这里只评价相对 REF 的误差，不代表绝对精度已获验证。

| 退出码 | 含义 |
| --- | --- |
| `0` | 普通评分完成；或 `--exam` 门槛通过 |
| `1` | 文件、模型加载或运行失败 |
| `2` | 参数、配置或数据集约定不合法 |
| `3` | 仅 `--exam`：考试未通过或待验收 |

## 9. 离线预测

```bash
truetemp predict data/new01/dut.csv --model models/dut01 --output predictions/new01.csv
```

| 参数 | 说明 |
| --- | --- |
| `INPUT` | DUT CSV，字段和单位与采集输出一致，必填 |
| `--model` | 模型目录，必填 |
| `--output` | 新 CSV 文件，必填 |

无需 REF。验证设备身份、协议和必需特征后，保留输入列并增加校正温度、物理补偿湿度、最终湿度、未裁剪湿度和质量标记。温湿度显示 1 位小数，模型内部不提前舍入。

上述新增命令均拒绝覆盖已有输出；具体数据契约及物理边界以 [系统设计](SYSTEM_DESIGN.md) 为准。

## 10. 实验标记、覆盖与训练配置

可复制 [实验清单](examples/experiment.toml) 和 [训练配置](examples/training.toml)。清单路径相对清单文件本身，复制后请检查。

每个 session 可设置 `phase = "steady"`（稳态）、`"transient"`（瞬态），省略为 `"unspecified"`。仅明确标注的稳态样本用于覆盖验收。可用 `exclude = [{start=0, end=30, reason="镜像初始化"}]` 排除区间：秒数相对该会话最早有效广播，左闭右开。配对不跨排除边界及检测到的 DUT uptime 回退。达到 uptime 上限后发生但未表现为回退的重启无法自动识别。

清单可含 `[coverage]`：`temperature_range` 默认 `[0,60]`；`humidity_range`、`conditions`、`min_samples_per_cell` 必填；`temperature_bin_width` 和 `humidity_bin_width` 默认均为 10。每个温度 × 湿度 × 工况组合需满足最小稳态样本数，复用的同一 REF 报文只计一次，质量异常或超出训练输入范围的样本不计。末个分箱包含范围上端点。相邻采样仍相关，该计数不代表统计独立重复实验数。

覆盖声明在训练时冻结于模型；考试不从新数据集改写门槛。未配置或覆盖不充分时不会返回正式考试通过。当前湿度范围尚待实验确认，示例中的 20～80%RH 不是项目已确定的范围。

`training.toml` 支持 `[training]` 下的 `learning_rate`、`max_depth`、`min_child_samples`、`max_iterations`、`early_stopping`、`num_leaves`（列表）、`reg_lambda`（列表）、`seed`、`num_threads`；默认值见示例。叶子数和正则化组合最多 16 组，仅用验证集比较。

`[features].disable` 是禁用特征名列表。原始温湿度即使禁用为树模型特征，仍是物理计算必需输入。输入 CSV 需保留完整 DUT 遥测字段、`version` 和 `device_id`。不会将缺失列或非有限值补零。

模型若选择常数残差，数值保存在 `metadata.json`，对应 `.txt` 不生成；否则保存 LightGBM 原生文件。`training.json` 记录实际候选参数和警告，`validation.json` 为验证指标。模型仅适用于绑定的 DUT 标识；设备标识改变不会自动迁移模型。
