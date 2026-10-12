/*
 * ============================================================
 * QingThermal 温湿耦合补偿 — 两阶段 C 推理 (RK3506)
 * ============================================================
 *
 * 文件清单（拷到编译目录下）:
 *   infer_coupled_c.c               本文件
 *   models/model_temperature.h      温度模型 C 头文件 (500棵树)
 *   models/model_vapor_pressure.h   水汽压残差 C 头文件 (500棵树)
 *
 * --- 编译 ---
 *
 *   本机测试:
 *     gcc -O2 -o infer_coupled_c infer_coupled_c.c -lm
 *
 *   RK3506 交叉编译 (Cortex-A7, double 版本):
 *     arm-linux-gnueabihf-gcc -O2 -mcpu=cortex-a7 -mfpu=neon-vfpv4 \
 *         -mfloat-abi=hard -o infer_coupled_c infer_coupled_c.c -lm
 *
 *   RK3506 推荐使用 float 版本以获得 NEON 4路并行加速:
 *     见 infer_coupled_c_f.c (精度差异 < 1e-5 °C)
 *
 * --- 运行 ---
 *
 *   ./infer_coupled_c
 *
 *   输出:
 *     Temperature:        27.68 C
 *     Relative Humidity:  52.92 %
 *     Dew Point:          17.22 C
 *     Absolute Humidity:  14.14 g/m3
 *
 * --- 集成到自己项目 ---
 *
 *   只需调用 predict_coupled()，不需要 main() 函数。
 *   删掉或注释掉本文件末尾的 main()，然后 #include 本文件（或把函数拷贝到你的代码中）。
 *
 *   #include "infer_coupled_c.c"
 *
 *   double T, RH, Td, AH;
 *   predict_coupled(
 *       raw_temperature,     // DUT 原始温度 (°C)
 *       raw_humidity,        // DUT 原始相对湿度 (%)
 *       temperature_soc,     // SOC 温度 (milli°C)
 *       temperature_co2,     // CO2 传感器温度 (milli°C)
 *       time_uptime,         // 设备运行时间 (秒)
 *       screen_on_duration,  // 屏幕亮屏时长 (秒)
 *       cpu_load_10s,        // CPU 10s 滑动负载 (milli)
 *       ambient_lux,         // 环境光 (lux)
 *       power_charge_ma,     // 充电电流 (mA)
 *       power_charge_mv,     // 充电电压 (mV)
 *       &T, &RH, &Td, &AH    // 输出指针
 *   );
 *   // T = 补偿后温度, RH = 联动修正相对湿度
 *   // Td = 露点, AH = 绝对湿度 (g/m³)
 *
 * --- 参数说明 ---
 *
 *   [输入, 10 个, 都是 double]
 *   raw_temperature       DUT 原始温度读数, 单位 °C, 如 33.4
 *   raw_humidity          DUT 原始相对湿度读数, 单位 %, 如 37.7
 *   temperature_soc       SOC 温度传感器读数, 单位 °C×1000, 如 526(=526000/1000)
 *                         (如果原始值是 526000 则填 526.0)
 *   temperature_co2       CO2 温度传感器读数, 同上
 *   time_uptime           设备持续运行时间, 单位 秒, 如 3171
 *   screen_on_duration    屏幕累计亮屏时长, 单位 秒, 如 2307
 *   cpu_load_10s          CPU 近 10 秒平均负载, unitless(milli), 如 139
 *   ambient_lux           环境光传感器读数, 单位 lux, 如 116
 *   power_charge_ma       电池充电电流, 单位 mA (0 表示未充电), 如 156
 *   power_charge_mv       电池充电电压, 单位 mV, 如 3937
 *
 *   [输出, 4 个, double*]
 *   out_temp              补偿后温度 (°C), 物理上应等于真实环境温度
 *   out_rh                联动修正后相对湿度 (%)，与 T_comp 满足同一水汽压
 *   out_dew               露点温度 (°C), e<0.06 hPa 时返回 NaN
 *   out_ah                绝对湿度 (g/m³)
 *
 * --- 物理原理 ---
 *
 *   Stage 1: LightGBM 预测补偿后温度 T_comp
 *   Stage 2: LightGBM 预测水汽压残差 δe
 *
 *   e_raw  = RH_raw / 100 × 6.112 × exp(17.67×T_raw / (T_raw+243.5))
 *   e_cor  = e_raw + δe                        ← 修正后水汽压 (hPa)
 *   RH_comp = clamp(100 × e_cor / es(T_comp), 0, 100)
 *   AH     = 216.7 × e_cor / (T_comp + 273.15)
 *   T_dew  = 243.5 × ln(e_cor/6.112) / (17.67 - ln(e_cor/6.112))
 *
 *   四项输出全部从 e_cor 和 T_comp 推导, 物理严格一致。
 *
 * --- 调用频率建议 ---
 *
 *   1~10 秒一次。跟传感器采样频率对齐即可，不需要更频繁。
 *   C 版本单次推理约 10~150 微秒 (取决于平台)，性能不是瓶颈。
 *
 * --- 模型精度 (训练集全量评估) ---
 *
 *   温度: R²=0.9991  RMSE=0.038°C  MAE=0.026°C
 *   湿度: R²=0.9981  RMSE=0.266%   MAE=0.151%
 */

#include <stdio.h>
#include <math.h>
#include "models/model_temperature.h"
#include "models/model_vapor_pressure.h"

#define CLAMP(x, lo, hi) ((x) < (lo) ? (lo) : (x) > (hi) ? (hi) : (x))

static double saturation_vapor_pressure(double T) {
    return 6.112 * exp(17.67 * T / (T + 243.5));
}

void predict_coupled(
    double raw_temperature, double raw_humidity,
    double temperature_soc, double temperature_co2,
    double time_uptime, double screen_on_duration,
    double cpu_load_10s, double ambient_lux,
    double power_charge_ma, double power_charge_mv,
    double* out_temp, double* out_rh, double* out_dew, double* out_ah)
{
    double x[MODEL_TEMP_NUM_FEATURES] = {
        raw_temperature, raw_humidity, temperature_soc, temperature_co2,
        time_uptime, screen_on_duration, cpu_load_10s, ambient_lux,
        power_charge_ma, power_charge_mv,
    };

    double T_comp = MODEL_TEMP_predict_one(x);

    double es_raw = saturation_vapor_pressure(raw_temperature);
    double e_raw = (raw_humidity / 100.0) * es_raw;

    double de = MODEL_DE_predict_one(x);
    double e_corrected = e_raw + de;

    double es_comp = saturation_vapor_pressure(T_comp);
    double RH_comp = CLAMP(100.0 * e_corrected / es_comp, 0.0, 100.0);

    double AH = 216.7 * e_corrected / (T_comp + 273.15);

    double T_dew = NAN;
    if (e_corrected > 0.06) {
        double gamma = log(e_corrected / 6.112);
        T_dew = 243.5 * gamma / (17.67 - gamma);
    }

    *out_temp = T_comp;
    *out_rh = RH_comp;
    *out_dew = T_dew;
    *out_ah = AH;
}

int main() {
    double T, RH, Td, AH;

    predict_coupled(
        33.4, 37.7, 526, 439, 3171, 2307, 139, 116, 156, 3937,
        &T, &RH, &Td, &AH
    );

    printf("Temperature:        %.2f C  (expected: 27.93)\n", T);
    printf("Relative Humidity:  %.2f %%  (expected: 52.23)\n", RH);
    printf("Dew Point:          %.2f C  (expected: 17.25)\n", Td);
    printf("Absolute Humidity:  %.2f g/m3 (expected: 14.16)\n", AH);

    return 0;
}
