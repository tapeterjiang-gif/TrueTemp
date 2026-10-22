# TrueTemp / QingThermal 温湿耦合补偿

校准 DUT 设备（sheet `2D58`）的温湿度传感器读数。设备因自发热（SoC 温度、充电、亮屏、CPU 负载等）导致原始读数偏离真实环境值，使用参考仪器（sheet `REF-50D6`）作为真值训练两阶段 LightGBM 补偿模型。

- **Stage 1**: LightGBM 预测补偿后温度 `T_comp`
- **Stage 2**: LightGBM 预测水汽压残差 `δe`，`e_corrected = e_raw + δe`
- 所有输出（RH、露点、绝对湿度）统一从 `e_corrected` 和 `T_comp` 推导，物理严格一致

模型精度（全量数据）：温度 RMSE 0.038°C / R²=0.9991；湿度 RMSE 0.266% / R²=0.9981。

## 目录结构

```
data/                      训练/测试数据（5 个 Excel，REF-50D6 + 2D58 两个 sheet）
models/                    模型文件（.pkl / .json / .h）
esp32/                     ESP32 集成（truetemp.h / truetemp.c / 生成的数据头文件）
train_coupled_model.py     两阶段训练
exam.py                    用 .pkl 模型评估（依赖 lightgbm）
exam_model.py              用 JSON 树模型评估（不依赖 lightgbm）
infer_coupled.py           纯标准库 Python 推理（RK PX30）
collect_data.py            BLE 采集 REF/DUT 广播并保存 xlsx
gen_c_model.py             生成 C 头文件（零依赖嵌入式部署）
infer_coupled_c.c          C 推理 double 版（RK3506）
infer_coupled_c_f.c        C 推理 float/NEON 版（RK3506）
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

无参数，固定读取 `data/` 下 5 个 Excel，产出到 `models/`：
`model_temperature_2stage.pkl`、`model_vapor_pressure.pkl`、
`model_temperature.json`、`model_vapor_pressure.json`、`model_2stage_metadata.json`

### 评估（考试）

```bash
# 用 .pkl 模型评估（依赖 lightgbm），参数为测试文件
python3 exam.py data/50D6_Data_241859.xlsx

# 用 JSON 树模型评估（不依赖 lightgbm），默认考 data/50D6_Data_241702.xlsx
python3 exam_model.py
```

### Python 推理

```bash
# 默认示例参数推理
python3 infer_coupled.py

# 传入 10 个参数: 原始温度, 原始湿度, soc温度(毫°C), co2温度(毫°C),
# 运行时长(s), 亮屏时长(s), cpu负载(milli), 环境光(lux), 充电电流(mA), 充电电压(mV)
python3 infer_coupled.py 33.4 37.7 526 439 3171 2307 139 116 156 3937
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

xlsx 保存格式与 20260912_2328.xlsx 一致：REF sheet（timestamp, temperature, humidity）+ DUT sheet（received_at 及 11 个字段）。文件以启动采集时间命名（data/年月日_时分.xlsx），采集过程中每满 50 条 REF 样本落盘一次，Ctrl+C 时最终保存。

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
python3 gen_c_model.py --esp32     # 输出 esp32/truetemp_model_data.h
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
- 模型数据 27,736 个节点、约 **325 KiB**，`static const` 放在 flash，不占内部 RAM
- 全程 float 运算（ESP32 无硬件 double），单次推理约 5000 次节点访问，1~10 秒调用一次即可
- 调度器/中断里没有动态内存分配和阻塞调用，可安全放在任务上下文调用

### C 推理编译（RK3506）

```bash
# 本机测试（double 版）
gcc -O2 -o infer_coupled_c infer_coupled_c.c -lm
./infer_coupled_c

# RK3506 交叉编译（Cortex-A7，float/NEON 版，推荐）
arm-linux-gnueabihf-gcc -O3 -mcpu=cortex-a7 -mfpu=neon-vfpv4 \
    -mfloat-abi=hard -ffast-math -funroll-loops \
    -o infer_coupled_c_f infer_coupled_c_f.c -lm
./infer_coupled_c_f
```

集成时调用 `predict_coupled()`（或 `predict_coupled_f()`）函数，删掉文件末尾的 `main()` 即可。
