# TrueTemp 温湿耦合补偿

校准 DUT 设备（sheet `2D58`）的温湿度传感器读数。设备因自发热（SoC 温度、充电、亮屏、CPU 负载等）导致原始读数偏离真实环境值，使用参考仪器（sheet `REF-50D6`）作为真值训练两阶段 LightGBM 补偿模型。

- **Stage 1**: LightGBM 预测补偿后温度 `T_comp`
- **Stage 2**: LightGBM 预测水汽压残差 `δe`，`e_corrected = e_raw + δe`
- 所有输出（RH、露点、绝对湿度）统一从 `e_corrected` 和 `T_comp` 推导，物理严格一致

模型精度（全量数据）：温度 RMSE 0.038°C / R²=0.9991；湿度 RMSE 0.266% / R²=0.9981。

## 目录结构

```
data/                      训练/测试数据（xlsx，REF + DUT 两个 sheet）
models/                    模型文件（.pkl / .json）
esp32/                     ESP32 集成（truetemp.h / truetemp.c / 生成的数据头文件）
linux/                     Rockchip 集成（PX30 / RK3506：C 接口 + Python 推理）
train_coupled_model.py     两阶段训练
exam.py                    用 .pkl 模型评估（依赖 lightgbm）
exam_model.py              用 JSON 树模型评估（不依赖 lightgbm）
collect_data.py            BLE 采集 REF/DUT 广播并保存 xlsx
gen_c_model.py             生成 C 头文件（零依赖嵌入式部署）
```

## 命令

### 环境准备

系统 `python3` 未装 pandas/lightgbm，训练/评估前需准备环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pandas lightgbm joblib scikit-learn openpyxl
```

### 训练

```bash
python3 train_coupled_model.py
```

无参数，自动读取 `data/` 下所有 Excel，产出到 `models/`：
`model_temperature_2stage.pkl`、`model_vapor_pressure.pkl`、
`model_temperature.json`、`model_vapor_pressure.json`、`model_2stage_metadata.json`

### 评估（考试）

```bash
# 用 JSON 树模型评估（不依赖 lightgbm），参数为测试文件；不传则用 data/ 下最新文件
python3 exam_model.py data/20261023_1948.xlsx

# 模拟 ESP32 部署模型（前 200 棵树）
python3 exam_model.py data/20261023_1948.xlsx --trees 200
```

### 数据采集

```bash
# 依赖: .venv 中安装 bleak（.venv/bin/pip install bleak）

# 快捷命令（自动用 .venv）:
./collect scan                                     # 扫描设备
./collect set                                      # 扫描后交互选择 REF/DUT，保存到 truetemp.toml
./collect collect                                  # 采集（读取 truetemp.toml 中的设备）
./collect collect --ref <REF_UUID> --dut <DUT_UUID> --interval 5   # 采集（显式指定设备，Ctrl+C 保存 xlsx）
./collect collect --demo 5                         # 无硬件模拟
./collect probe <UUID>                             # 监听单设备广播并枚举服务（诊断）

# 等价完整写法（或先 source .venv/bin/activate 再直接用 python）:
.venv/bin/python collect_data.py scan
.venv/bin/python collect_data.py collect --ref <REF_UUID> --dut <DUT_UUID>
python3 collect_data.py collect --demo 5           # demo 无需 bleak
```

XLSX 中的 `REF` 和 `DUT` 按 `--interval` 记录每个窗口内最新的有效读数。`REF_RAW` 和 `DUT_RAW` 逐条记录有效广播，包含实际接收时间、单调时钟毫秒数和原始载荷。文件以采集开始时间命名，REF 或 DUT 每满 50 条窗口样本落盘一次，按 Ctrl+C 最终保存。

### 生成 C 头文件

```bash
# 默认生成 double + float 两个版本
python3 gen_c_model.py

# 单文件生成: python3 gen_c_model.py <json_in> <h_out> <name>
python3 gen_c_model.py models/model_temperature.json models/model_temperature.h MODEL_TEMP

# 加 --float 生成 NEON 优化 float 版
python3 gen_c_model.py --float
```

### ESP32 集成

重新训练模型后，先重新生成 ESP32 模型数据：

```bash
python3 gen_c_model.py --esp32              # 默认 200 棵树/模型，约 87 KiB
python3 gen_c_model.py --esp32 --trees 150  # 更小，约 65 KiB
python3 gen_c_model.py --esp32 --trees 500  # 保留完整模型，约 218 KiB
```

集成步骤：

1. 把 `esp32/truetemp.h`、`esp32/truetemp.c`、`esp32/truetemp_model_data.h` 加入工程。
   - ESP-IDF：放进 `main/` 组件目录即可
   - Arduino-ESP32：放进 sketch 目录
2. 调用 `truetemp_compensate()`：

```c
#include "truetemp.h"

truetemp_input_t in = {
    .raw_temperature = 26.1f, .raw_humidity = 25.3f,   // 传感器原始读数
    .temperature_soc = 38.2f, .temperature_co2 = 32.0f,
    .time_uptime = 7200.0f, .screen_brightness = 80.0f,
    .screen_on_duration_s = 0.0f, .cpu_load_10s = 11.0f,
    .wifi_load_10s = 3.0f, .power_charge_ma = 0.0f,
    .c5_soc_temp = 30.9f,
};
truetemp_output_t out;
truetemp_compensate(&in, &out);
// out.temperature, out.humidity, out.dew_point, out.absolute_humidity
```

说明：

- 输入单位必须与训练数据一致：温度 °C、湿度 %、时间 秒、负载 %、充电电流 mA
- 模型数据默认 11,176 个节点、约 **87 KiB**（200 棵树/模型，int16 量化），`static const` 放在 flash，不占内部 RAM
- 叶子值与阈值量化为 int16，相比 float 版最大误差约 0.008°C；200 棵树与 500 棵树的留出集 RMSE 基本一致
- 全程 float 运算（ESP32 无硬件 double），单次推理约 2000 次节点访问，1~10 秒调用一次即可
- 调度器/中断里没有动态内存分配和阻塞调用，可安全放在任务上下文调用

### Linux 集成（Rockchip PX30 / RK3506）

Rockchip Linux 推理使用 float 树模型。默认目标使用既有 DUT v3 模型。

#### Snow2S v4

先用 `./collect collect --interval 10` 同时采集参考表与 Snow2S v4 DUT。训练优先读取同一 XLSX 中的原始接收记录，只选 `version=4` 的 DUT 行。训练合并 10 毫秒内载荷相同的重复回调，在 ±2 秒内优先匹配时间差最小的 REF/DUT，每条记录最多用一次。早期 XLSX 沿用同秒配对。
背光按原始 0～1000 输入，Wi-Fi 特征为
`wifi_tx_bytes`（Byte/s）。C5 温度和旧 Wi-Fi 负载百分比不属于此模型。

```bash
python3 train_coupled_model.py --snow2s data/你的采集文件.xlsx
python3 linux/infer_truetemp.py --snow2s 26.1 25.3 38.2 32.0 7200 1000 120 11 1800 0
make -C linux snow2s
```

训练结果默认写入 `models/snow2s/`，其中 `pairs.csv` 记录每对的来源文件、配对方式和接收时间差。早期 XLSX 的实际接收时间差留空。元数据包含各特征的实测范围。Python 与 C 按相同顺序
读取 10 个 v4 字段。上例中 `1000` 是背光，`1800` 是 Wi-Fi 发送速率。
C 目标从这对 JSON 生成 `truetemp_snow2s_model_data.h` 并产出 `truetemp_infer_snow2s`。
直接集成 C 时，编译库和调用方都定义 `TRUETEMP_SNOW2S`，调用 `truetemp_compensate_snow2s`。
训练与导出需要先提供实际 v4 数据，已有 v3 模型不能作为 v4 模型加载。

#### Snow2S 温差回退候选版（20261113）

候选代码和模型在 `models/snow2s-20261113-hybrid/`。原始温度不高于 26℃ 时使用
`20261112` 的绝对温度模型；高于 26℃ 时使用其余 9 项输入预测
`delta = raw_temperature - REF temperature`，输出 `raw_temperature - delta`。
四项热状态模型使用 SoC 温度、CO₂ 模块温度、亮度和充电电流预测温差。当该预测值
不高于 3.5℃ 时，执行 `delta -= 0.75 * max(delta - 2.6, 0)`。阈值随模型一起导出。
湿度、露点和绝对湿度按最终温度与水汽压计算。

从保存的配对表生成一个新候选目录：

```bash
.venv/bin/python train_snow2s_hybrid.py \
  --base-dir models/snow2s-20261112 \
  --output-dir models/snow2s-hybrid-new
```

训练重放原配对表指定的 4,611 对记录，使用源文件内两侧时间戳回查，不重新扩大配对。
`training_pairs.csv` 保存 REF 温湿度、原始输入及来源；元数据保存训练参数、软件版本、
原始文件和代码 SHA-256。原绝对温度树和水汽压模型保留在候选包中。

编译已生成的候选模型：

```bash
make -C linux snow2s SNOW2S_MODEL_DIR=../models/snow2s-20261113-hybrid
python3 linux/infer_truetemp.py --snow2s --model-dir models/snow2s-20261113-hybrid \
  27 32.3 55.1 45.3 572 980 466 7.7 241 0
```

接入 Snow2SApp 时一起使用候选目录中的 `truetemp.c`、`truetemp.h` 和
`truetemp_snow2s_model_data.h`，编译库和调用方都定义 `TRUETEMP_SNOW2S`，调用
`truetemp_compensate_snow2s()`。输入字段顺序、单位和函数签名与现有 Snow2S 相同。
新模型头会检查运行时代码版本，旧推理代码不能单独搭配新头文件使用。

这是本机验证的候选版本。低于训练下限时沿用绝对温度模型，未加入低温回退。
26℃ 上限采用直接切换；77 条接近上限的历史工况在边界处的输出跳变量约
0.018～0.585℃。27℃ 模拟的输出中位数约 22.51℃，尚未证明可纠正现场 24.x℃ 的偏差。
`validation.json` 记录本机 C/Python 复算误差及边界结果，实机精度需用完整现场输入验证。

#### Snow2S 实机对比版：阈值 4.5℃、少扣 100%

独立候选包在 `models/snow2s-20261113-gate45-shrink100/`，原候选包保留用于对比。
新包的模型树和配对数据与原候选包一致，只将 `heat_gate_max` 改为 `4.5`、
`shrink_fraction` 改为 `1.0`。原始温度高于 26℃ 且四项预测温差不高于 4.5℃ 时，
使用 `min(预测温差, 2.6)` 扣减；其余情况沿用原分支逻辑。

可复现生成命令（输出目录须不存在）：

```bash
.venv/bin/python train_snow2s_hybrid.py \
  --base-dir models/snow2s-20261112 \
  --output-dir models/snow2s-20261113-gate45-shrink100 \
  --heat-gate-max 4.5 --shrink-fraction 1.0
```

编译和本机试算：

```bash
make -C linux snow2s SNOW2S_MODEL_DIR=../models/snow2s-20261113-gate45-shrink100
./linux/truetemp_infer_snow2s 27 32.3 55.1 45.3 572 980 466 7.7 241 0
```

该示例输出 `temperature=24.4000`。实机接入使用新包中的 `truetemp.c`、`truetemp.h`、
`truetemp_snow2s_model_data.h`，编译库和调用方均定义 `TRUETEMP_SNOW2S`。
完整接入说明见候选包 `README.md`，本机验证结果见 `validation.json`。
此版仍在 26℃ 处直接切换；77 条接近上限的历史工况在边界处的最大输出跳变约 2.301℃。

#### 既有 v3

重新训练后生成 Linux 模型：

```bash
python3 gen_c_model.py --linux               # 500 棵树/模型，约 332 KiB .rodata
python3 gen_c_model.py --linux --trees 200   # 限制体积，约 133 KiB
```

`linux/` 目录内容：

```
linux/truetemp.h             Rockchip Linux 对外接口
linux/truetemp.c             float 推理实现，只依赖 libm
linux/truetemp_model_data.h  由 --linux 生成，不要手改
linux/truetemp_infer.c       命令行示例（11 个参数或 stdin）
linux/infer_truetemp.py      纯标准库 Python 推理（读取 models/*.json）
linux/Makefile               本机与交叉编译入口
```

本机编译与验证：

```bash
cd linux
make
./truetemp_infer 26.1 25.3 38.2 32.0 7200 80 0 11 3 0 30.9
# temperature=... humidity=... dew_point=... absolute_humidity=...
```

交叉编译（`CC` 前缀按实际 Buildroot / Linaro SDK 替换）：

```bash
# PX30（Cortex-A35，aarch64）
make clean
make CC=aarch64-linux-gnu-gcc CFLAGS="-O3 -std=c11 -mcpu=cortex-a35"

# RK3506（Cortex-A7，armhf）
make clean
make CC=arm-linux-gnueabihf-gcc \
     CFLAGS="-O3 -std=c11 -mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard -ffast-math"
```

集成 v3 模型时，把 `truetemp.h`、`truetemp.c`、`truetemp_model_data.h` 加入工程并链接 `-lm`。

```c
truetemp_input_t in = { /* 11 个字段，单位与训练数据一致 */ };
truetemp_output_t out;
truetemp_compensate(&in, &out);
```

设备上已有 Python 3 时，也可以直接用标准库推理（无需 numpy / lightgbm）：

```bash
python3 linux/infer_truetemp.py 26.1 25.3 38.2 32.0 7200 80 0 11 3 0 30.9
python3 linux/infer_truetemp.py --trees 200 26.1 25.3 38.2 32.0 7200 80 0 11 3 0 30.9
```

说明：

- 模型数据约 28,316 个节点、**332 KiB** `.rodata`；本机验证二进制约 372 KB
- 全流程 float 运算，Cortex-A7 / Cortex-A35 均可用 NEON
- 重新训练后必须重新执行 `--linux`（或 `make model`），否则头文件与 JSON 模型不一致
- 不要把 ESP32 的 `truetemp_model_data.h` 与 Linux 版混用：前者是 int16 量化格式，后者是 float 格式

## truetemp CLI 包（src/truetemp）

仓库同时保留了基于 `src/truetemp` 的命令行实现，覆盖采集 → 数据集 → 训练 → 考试 → 预测的完整流程：

```bash
uv sync --python 3.12
source .venv/bin/activate
truetemp scan       # 扫描并选择 REF / DUT
truetemp collect    # 采集会话目录（raw.jsonl / reference.csv / dut.csv / session.json）
truetemp dataset build experiment.toml --output datasets/dut01
truetemp train datasets/dut01 --output models/dut01
truetemp evaluate datasets/dut01 --model models/dut01 --output reports/exam01 --exam
truetemp predict data/new01/dut.csv --model models/dut01 --output predictions/new01.csv
```

更多说明见 [GUIDE.md](GUIDE.md)、[CLI.md](CLI.md)、[DUT_PROTOCOL.md](DUT_PROTOCOL.md)、[SYSTEM_DESIGN.md](SYSTEM_DESIGN.md)。
