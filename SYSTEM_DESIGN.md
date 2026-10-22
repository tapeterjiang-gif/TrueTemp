# TrueTemp 系统设计

版本：v1.0 · 日期：2026-09-10 · 状态：首版软件已实现，实机精度待验收

## 1. 目标与边界

在当前 Mac（Apple Silicon，Python 3.12）上，通过 BLE 采集一台 REF 和一台 DUT，离线训练 LightGBM 模型，完成温度校正、湿度物理补偿与算法校正，并使用独立数据集考试。

- REF：Sensirion SHT4x Smart Gadget；DUT：0xDFDF v3，详见 [协议](DUT_PROTOCOL.md)。
- 默认采集间隔 5 秒；扫描接收持续运行。没有新数据时留缺。
- 面向同一型号所有 DUT 单元建立通用标定模型，训练、考试和预测都在本机运行。模型不绑定单台设备；推理仅按 DUT 协议版本校验输入。
- 目标范围 0～60°C；湿度覆盖范围待定。
- 用户给定参考精度 ±0.2°C、±2%RH；DUT 期望精度 ±0.3°C、±3%RH。
- 本阶段不做云服务、GUI、固件部署、在线学习或环境箱控制。

已实现：扫描、设备配置、REF/DUT 采集、CSV 导出、数据集构建、物理计算、训练、考试、预测。现有采集命令保持兼容。

## 2. 总体流程

```text
REF 广播 ─→ REF 原始日志 ─┐
                         ├→ 重建采样记录 → 时间配对 → 数据集
DUT 广播 ─→ DUT 原始日志 ─┘                  │
                                            ├→ 训练集：拟合模型
                                            ├→ 验证集：早停、选择参数
                                            └→ 考试集：冻结模型后评分

DUT 输入 ─→ 特征构建 ─→ 温度 LightGBM ─→ 校正温度 T̂
    │                                          │
    └→ 原始水汽压 e₀ ─→ 水汽压残差 LightGBM ─→ ê ─→ RĤ = 100 ê / es(T̂)
                         │
                         └→ 同时输出仅物理补偿结果 RHphys = 100 e₀ / es(T̂)
```

预测链不读取 REF；REF 只用于训练目标与评分。训练、考试、预测共用同一个特征和物理计算实现。

## 3. 模块与接口

保留当前包结构，按功能新增模块，避免把训练逻辑塞入 CLI。

| 模块 | 职责与主要接口 |
| --- | --- |
| `protocol.py` | REF/DUT 字节解析，保留现有接口 |
| `ble.py` | 广播采集、终端状态和持久化 |
| `config.py` / `cli.py` | 配置验证、命令入口和错误提示 |
| `export.py` | 用户 CSV 导出，保留当前行为 |
| `dataset.py` | `build_dataset(manifest, output)`：原始数据重建、质量检查、配对 |
| `features.py` | `build_features(dut_records, spec)`：纯 DUT 特征，训练与预测共用 |
| `physics.py` | 饱和水汽压、RH/水汽压互换与物理检查 |
| `training.py` | `train(dataset, config, output)`：基线、两个模型、验证集选型 |
| `calibration.py` | `load_bundle(path)`、`predict(records)`：统一推理管线 |
| `evaluation.py` | `evaluate(bundle, dataset, split, output)`：考试和报告 |

新增依赖为 NumPy、pandas、LightGBM；用现有 `uv.lock` 固定版本。模型使用 LightGBM 原生模型文件和 JSON 元数据，不依赖 pickle。

## 4. 数据与精度约定

### 4.1 保留当前产物

- REF：`raw.jsonl`、`reference.csv`、`session.json`。
- DUT：`dut_raw.jsonl`、`dut.csv`、`dut_session.json`。
- 用户 CSV 和终端：温湿度保留 1 位小数，`received_at` 保留到秒；CSV 不显示单调时钟。
- 原始日志保存完整厂商载荷和单调时钟，支持重新解析及诊断。

### 4.2 建模数据来源

默认从原始日志重新解析，保留浮点计算精度：REF 不使用已舍入的 CSV 值，DUT 仍只有协议提供的 0.1 单位精度，不制造额外测量精度。

同一会话的两台设备共享主机单调时钟，用它做精确配对。只在会话内部比较该时钟，不跨主机或不同会话直接比较。秒级 UTC 时间用于显示和追溯。

首版训练要求有原始日志；只有 CSV 的历史数据先不支持训练导入，避免无声改变时间和数值精度。预测可接受当前导出的 DUT CSV，并在结果标记输入来源。

### 4.3 内部数据集

构建时生成内部 CSV（保留计算精度，不走用户 `export` 舍入逻辑），包含：

- `sample_id`、`session_id`、`experiment_id`、`condition_id`、`split`。
- REF/DUT 设备标识、原始日志行号、各自接收时间与单调时钟。
- DUT 所有解析参数、REF 温湿度、`pair_gap_seconds`。
- 质量状态、原始/变换值来源；特征列由显式白名单决定。

内部 CSV 的 sample_id 由会话来源指纹与原始行号生成，不以秒级时间单独作唯一键。原始载荷、ID、时间、split 和 REF 字段均不得误作模型特征。

数据集清单可包含多台同型号 DUT 单元的会话，样本合并训练同一通用模型；不要求整份数据集对应同一对 REF/DUT。各会话的设备标识仅作溯源保留在元数据与 source 记录中。

## 5. 实验清单与时间配对

### 5.1 显式清单

用户通过 YAML/JSON 二选一会增加依赖与规则，首版统一使用已有 TOML：

```toml
# experiment.toml：路径相对此清单所在目录
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

示例只演示格式，不构成充分工况覆盖。每条 session 可选指定排除时间区间及原因；多工况首版通过分别采集会话表示，不增加运行中标注命令。

同一次连续实验即使拆成不同文件，也使用同一 `experiment_id`，必须归属同一 split。温湿度工况可以跨集合重复，但应是独立重复实验。训练需非空 train/validation，考试需非空 test；可先构建不含 test 的开发数据集或仅含 test 的独立考试数据集。

### 5.2 配对规则

1. 在每个会话中独立读取两台设备，按单调时钟排序；检查协议和设备身份。
2. 以该会话两份原始日志最早接收时刻为共同起点，按会话元数据的 interval_seconds（默认 5 秒）网格重建采样，各区间每台设备取最新有效报文。结束时保留不足 5 秒的最后区间；不向空区间填值。
3. 对每条 DUT 样本选择最近的 REF 样本，最大时间差默认 2 秒；等距时选择较早 REF。
4. 允许一个 REF 被多个 DUT 使用，但保存复用次数；训练权重按 REF 复用次数的倒数修正，避免重复参考值被过度放大。
5. 不跨会话、明确重启区间、人工排除区间配对；不自动插值或把容差扩大到广播缺口。
6. 报告接收数、重建样本数、有效配对数、剔除原因、复用率及时间差分布。容差修改后必须重建数据集，模型保存生效规则。

主机接收时间不是测量时间，配对只能缓解错位，无法消除传感器响应滞后。首版稳态工况单独评分；升降温与亮灭屏等瞬态作为单独分组，不与稳态合并宣称精度。

## 6. 特征设计

首版使用当前 DUT 参数，不引入历史窗口，保证文件预测与训练容易保持一致。热历史特征作为后续增量实验，不阻塞首版。

| 特征 | 处理 |
| --- | --- |
| `raw_temperature`、`raw_humidity` | 原始未校正工程量 |
| `temperature_co2` | 内部温度；确认镜像有效后使用 |
| `time_uptime` | 启动后秒数；保留饱和值，不推算超过上限的时间 |
| `screen_brightness`、`screen_on_duration_s` | 原始级数、秒数 |
| `cpu_load_10s`、`wifi_load_10s` | P4/C5 负载百分比 |
| `power_charge_ma` | 有符号电流，允许负值 |
| `co2_temperature_delta` | `temperature_co2 - raw_temperature` |
| `temperature_soc` | 数据源未实现，默认从特征中排除 |

水汽压模型额外使用由原始温湿度算出的 `e_raw_hpa`；不使用温度模型输出、REF 数据或由 REF 构造的特征。

镜像占位规则：不把所有 0 当作缺失，尤其 0°C、灭屏、零负载、未充电都可能是真值。缺少有效性位时通过实验准备和清单排除已知初始化区间；无法确认可用的镜像特征可整体禁用，并同步禁用派生特征。模型保存实际特征列表，考试与预测严格检查。训练集常量列记录并移除；不根据考试集决定特征。

## 7. 温度与湿度校正模型

### 7.1 温度模型

```text
yT = Tref - Traw
ΔT = LightGBM_T(X)
T̂ = Traw + ΔT
```

采用回归平方损失，验证集以温度 MAE 早停。内部保留全精度，不在中间步骤舍入。

### 7.2 物理关系

设 `es(T)` 为同一约定下的饱和水汽压（hPa）：

```text
e_raw = RHraw / 100 × es(Traw)
RHphys = 100 × e_raw / es(T̂)
       = RHraw × es(Traw) / es(T̂)
```

物理补偿假设传感器与参考环境的水汽分压基本一致，温度差主要来自自热等因素。使用同位置传感器的 `Traw`；不能用 CO₂ 内部温度代替。凝露、明显湿度梯度、快速瞬态另行标记。

饱和水汽压统一选择 Sonntag 1994 液态水公式，包括低于 0°C 时的过冷水基准；不在 0°C 自动切换冰面 RH。该基准需在硬件资料和实验中核对，冻结到模型元数据。计算函数支持原始自热温度高于 60°C；模型宣称有效的环境范围仍由实验验证。物理函数首版支持 −40～125°C，并拒绝非有限值和范围外输入；这只是计算支持范围，不是标定有效范围。

```text
K = T + 273.15
ln(es_hPa) = -6096.9385/K + 16.635794
             - 0.02711193*K + 0.00001673952*K²
             + 2.433502*ln(K)
```

公式及单位依据 [NCAR 水汽压公式资料](https://www.eol.ucar.edu/data-software/conventions-and-standards/water-vapor-pressure-formulations)。常压、近似相同压力为首版假设；压力相关增强因子暂不单独建模。

### 7.3 湿度算法：修正水汽压残差

为保留物理补偿结构，将之前“RH 残差校正”的设计细化为水汽压残差校正，仍使用第二个 LightGBM：

```text
e_ref = RHref / 100 × es(Tref)
ye = e_ref - e_raw
Δe = LightGBM_e(X, e_raw)
e_candidate = e_raw + Δe
ê = max(0, e_candidate)
RĤ_unclipped = 100 × ê / es(T̂)
```

第二个模型修正传感器湿度误差等剩余偏差，不直接输出任意 RH。该设计满足：在 `ê` 固定时，提高 `T̂` 会降低 RH。它不保证任意改变亮度、温度等输入时水汽压仍保持不变，也不宣称消除了凝露或湿度滞后。

湿度模型输入和标签不依赖温度模型训练内预测，因此不需要为构造湿度训练集额外做温度交叉拟合。湿度早停在验证集使用已选定温度模型的 `T̂`，将每轮 `Δe` 转成 RH 后计算 MAE。验证集用于选择，不作为考试分数。

训练水汽压模型时可按 `(100 / es(Tref))²` 加权，将水汽压平方误差换算为以参考温度计算的 RH 平方误差权重，再乘以配对复用权重并在训练集归一化；这不等同于最小化包含温度模型误差的最终 RH 损失，最终仍以完整验证管线评分。

### 7.4 边界输出

- 选定的考试集全部数据用于考试（仅按声明量程与物理合理性过滤，不按训练特征范围剔除）；`out_of_training_range` 仅作为诊断标记保留在预测结果中。
- `e_candidate < 0` 时保留该值，预测使用非负投影，并标记 `negative_vapor_pressure`。
- `RĤ_unclipped > 100` 时标记 `supersaturation`；用户展示值限制在 0～100，但保留未裁剪值。
- 报告同时呈现保护前候选、物理投影后未裁剪 RH、展示 RH 的指标；正式考试依据未裁剪 RH，并把非物理候选计为质量失败，不用裁剪掩盖问题。
- 没有验证依据时不随意设置固定“最多补偿几度/几%RH”的限制；任何限幅规则必须在验证阶段确定并写入模型，不能看考试结果后调整。

## 8. LightGBM 训练策略

首版固定小规模参数比较，CPU 运行，记录版本、种子和线程数。以下为起始值，不是精度保证：

| 参数 | 温度/水汽压模型起点 |
| --- | --- |
| boosting / objective | `gbdt` / `regression` |
| `learning_rate` | 0.03 |
| `num_leaves` / `max_depth` | 7 / 3 |
| `min_child_samples` | 20 |
| `reg_lambda` | 5 |
| 最大迭代 / 早停轮数 | 1000 / 50 |
| 随机种子 | 42 |

样本不足以支持分裂时明确警告、输出常数基线结果，不自动降低约束以追求训练拟合。通过验证集比较少量叶子数和正则化组合；不同时引入多种模型家族。

至少保存三个比较对象：未经校正、训练集平均偏差校正、最终 LightGBM 管线。湿度另外保存只有物理补偿的结果。模型不因训练误差低而自动判定合格。

LightGBM 的复杂度控制和早停参见 [官方调参说明](https://github.com/lightgbm-org/LightGBM/blob/main/docs/Parameters-Tuning.rst)。

## 9. 独立数据集考试

### 9.1 隔离与防泄漏

数据集清单必须有独立 train、validation、test 实验组。目录名不同不等于数据独立：保存规范化源文件内容 SHA-256、源样本标识、设备及实验组标识；拒绝跨集合的重复源文件/样本、相同实验组及相同设备时间区间重叠。必要的物理状态历史不得跨集合继承。

训练命令只加载 train 和 validation，不读取 test 的数值、分布或指标。模型元数据记录见过的实验/来源。考试前检查与这些来源是否重复；考试数据绝不进入早停、特征选择或调参。

反复查看同一考试集并据此修改模型，会把它变成开发数据。工具记录每次考试的模型和数据指纹；修改后需新的独立考试集才能作为最终验收。自动检查不能识别人为隐瞒的同次实验拆分，必须正确填写实验组。

### 9.2 报告

- 阶段：原始温湿度 → 温度校正/湿度仅物理补偿 → 最终校正。
- 每阶段：MAE、RMSE、有符号平均偏差、最大绝对误差、阈值内样本比例。
- 温度阈值 0.3°C，RH 阈值 3 个百分点；另报温湿度同时达标比例。
- 按温度区间、湿度区间、工况、会话统计，各组附样本量；稀疏组明确标注。
- 配对覆盖率、缺失率、非物理预测数、范围外样本数；不只展示成功样本。
- 稳态与瞬态分开报告。时间序列相邻样本相关，不能把大量重复样本当成独立精度证据。

### 9.3 结论语义

首版参考误差门槛采用有效考试样本最大绝对误差：温度 ≤0.3°C 且 RH ≤3%RH，质量失败为 0。范围外样本单列评分，不纳入范围内合格结论。

同时输出覆盖结论：湿度目标范围未确定、声明工况未全部覆盖或没有足够配对时为“覆盖不足/待验收”。即使已测样本全部达标，也不能宣布全范围达标。

以上只验证相对 REF 误差。REF ±0.2°C / ±2%RH 本身的误差不能忽略，不从目标阈值直接相减得到训练阈值。绝对精度声明需确认 REF 全范围校准及不确定度、布置和环境均匀性；报告单独标注“绝对精度未验证”。

## 10. 物理一致性检查

由解析单元测试与独立考试报告共同覆盖：

1. `T̂ = Traw` 且湿度模型残差为零时，RH 恢复原值。
2. 固定正水汽压时，0～60°C 内 RH 随温度上升而下降。
3. 原始温度因自热偏高，向下校正温度后 RHphys 增大。
4. RH 与水汽压互换可逆；°C/K、hPa/Pa 不混用。
5. 液态水基准在 0°C 连续；边界、零湿度、负温度和过饱和有明确输出。
6. 针对完整模型记录热状态切换前后误差，不把树模型阶梯跳变或瞬态偏差误当作物理保证。

物理公式满足关系不等于模型测量准确，两个结果分别报告。

## 11. CLI 与产物

新增命令：

```bash
# 一次构建显式划分的数据集，报告配对质量
truetemp dataset build experiment.toml --output datasets/dut01

# 只读取训练和验证数据
truetemp train datasets/dut01 --output models/dut01 --config training.toml

# 固定模型，在 test split 考试；--exam 使未通过时返回 3
truetemp evaluate datasets/dut01 --model models/dut01 --output reports/exam01 --exam

# 新考试批次可另建仅包含 test 的数据集，再用同一命令考试
# 离线预测无需 REF
truetemp predict data/new01/dut.csv --model models/dut01 --output predictions/new01.csv
```

`dataset build` 可接受只含 test 的清单，训练命令则必须同时有 train/validation。`train --config` 可省略以使用版本化默认值。所有输出路径已有内容时拒绝覆盖。

考试默认只报告；`--exam` 启用门槛退出码：0 为参考误差与覆盖门槛通过，3 为未通过或待验收，1 为运行失败，2 为参数错误。精度门槛不影响普通采集命令。

```text
 datasets/dut01/
   manifest.json         来源、hash、分组、规则及单位
   train.csv             内部全精度配对数据
   validation.csv
   test.csv
   quality.json          各集合数据质量；train 不读取 test 部分

 models/dut01/
   temperature.txt       LightGBM 温度修正模型
   vapor_pressure.txt    LightGBM 水汽压残差模型
   metadata.json         模型版本、训练用到的 DUT 单元溯源、协议、特征、物理公式、范围、数据指纹
   training.json         生效参数、版本、种子、最佳轮次、基线
   validation.json       验证指标，不含考试结果

 reports/exam01/
   report.md             可读报告
   metrics.json          全精度指标、门槛与结论
   predictions.csv       逐样本各阶段预测、误差、配对时间差和质量标记
```

预测输出保留原输入，并增加 `corrected_temperature`、`humidity_physical`、`corrected_humidity`、`humidity_unclipped`、水汽压候选/投影值、模型 ID 和质量标记。标准展示温湿度仍为 1 位小数；评分使用未舍入数据，`metrics.json` 保留精度，不能用展示 CSV 直接精确复算全精度分数。

模型加载验证 schema、协议版本（DUT v3）、特征/单位、物理公式。缺少必需输入时明确失败，不静默补 0；模型不校验具体设备身份，常量/缺失特征策略冻结到模型。

## 12. 测试与实施顺序

| 阶段 | 交付 | 验证 |
| --- | --- | --- |
| 1 | `physics.py`、`dataset build` | 公式数值点、单调性、时间错位/缺口、重复来源及跨组拒绝 |
| 2 | 特征管线、基线、温度 LightGBM | 仅 DUT 输入、独立验证、模型重载预测一致 |
| 3 | 水汽压 LightGBM 与完整推理 | 物理补偿、负水汽压/过饱和、零占位处理、预测不依赖 REF |
| 4 | `evaluate` 与报告 | 人工可核对误差、各阶段同样本比较、数据泄漏拒绝、考试退出码 |
| 5 | `predict`、端到端实机数据验收 | 新会话采集到独立考试全流程，兼容现有 REF-only 和导出测试 |

合成数据仅验证软件正确性，不用于证明校正精度。现有 31 项测试继续运行；新增测试围绕真实失效场景和算法约束，不以训练数据上的优秀分数代替验收。

## 13. 开发前仍需收敛的实验信息

- 相对湿度覆盖范围和要声明通过的工况列表。
- REF 与 DUT 探头放置方式、稳定判定以及独立重复实验安排。
- 镜像字段的就绪时间或有效性标志；计时上限及重启语义。
- 确认 RH 为相对液态水的定义。

以上影响精度声明和数据筛选，不阻塞基础公式、数据管线和模型接口开发。首版软件已实现；合成数据测试不构成实机标定精度验收。

## 14. 首版实现约定

- 可运行配置见 `examples/experiment.toml`、`examples/training.toml`，详细字段见 CLI 第 10 节。
- 常数残差也参加验证集选型；胜出或样本不足时保存 JSON 常数，不生成对应 LightGBM 文本文件。
- `phase` 默认 unspecified；覆盖只统计 steady、范围内且无质量标记的记录。每个声明的温度 × RH × 工况格子都需达到指定参考样本数，复用 REF 不重复计数。
- 训练时冻结覆盖声明；独立考试数据集不能修改已保存门槛。所有分组指标和候选/投影/裁剪阶段指标写入 metrics.json。
- 已知排除区间相对最早有效广播，以秒表示。运行中会话、单调时钟倒退和零配对会话拒绝构建。
