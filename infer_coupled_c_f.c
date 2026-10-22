/*
 * ============================================================
 * 温湿耦合补偿 — C 推理 float 版 (RK3506 专用)
 * ============================================================
 *
 * 为 RK3506 / Cortex-A7 NEON 优化。所有浮点使用 float (32-bit)，
 * NEON 可以 4 路并行加速，比 double 快约 2 倍。
 *
 * 文件清单（拷到编译目录下）:
 *   infer_coupled_c_f.c             本文件
 *   models/model_temperature_f.h    温度模型 C 头文件 (Float, 500棵树)
 *   models/model_vapor_pressure_f.h 水汽压残差 C 头文件 (Float, 500棵树)
 *
 * --- 编译 ---
 *
 *   本机测试:
 *     gcc -O2 -o infer_coupled_c_f infer_coupled_c_f.c -lm
 *
 *   RK3506 交叉编译 (Cortex-A7, NEON 4-way SIMD):
 *     arm-linux-gnueabihf-gcc -O3 -mcpu=cortex-a7 -mfpu=neon-vfpv4 \
 *         -mfloat-abi=hard -ffast-math -funroll-loops \
 *         -o infer_coupled_c_f infer_coupled_c_f.c -lm
 *
 * --- 精度说明 ---
 *
 *   Float 版与 Double 版精度对比:
 *     Temperature diff: < 1e-5 °C
 *     RH diff:          < 4e-5 %
 *     远小于模型 RMSE (0.03°C / 0.23%)，实际使用无差异。
 *
 * --- 模型精度 (训练集全量评估) ---
 *
 *   温度: R²=0.9941  RMSE=0.034°C  MAE=0.023°C
 *   湿度: R²=0.9945  RMSE=0.231%   MAE=0.143%
 */

#include <stdio.h>
#include <math.h>
#include "models/model_temperature_f.h"
#include "models/model_vapor_pressure_f.h"

#define CLAMP(x, lo, hi) ((x) < (lo) ? (lo) : (x) > (hi) ? (hi) : (x))

static float saturation_vapor_pressure(float T) {
    return 6.112f * expf(17.67f * T / (T + 243.5f));
}

void predict_coupled_f(
    float raw_temperature, float raw_humidity,
    float temperature_soc, float temperature_co2,
    float time_uptime, float screen_on_duration,
    float cpu_load_10s, float ambient_lux,
    float power_charge_ma, float power_charge_mv,
    float* out_temp, float* out_rh, float* out_dew, float* out_ah)
{
    float x[MODEL_TEMP_F_NUM_FEATURES] = {
        raw_temperature, raw_humidity, temperature_soc, temperature_co2,
        time_uptime, screen_on_duration, cpu_load_10s, ambient_lux,
        power_charge_ma, power_charge_mv,
    };

    float T_comp = MODEL_TEMP_F_predict_one_f(x);

    float es_raw = saturation_vapor_pressure(raw_temperature);
    float e_raw = (raw_humidity / 100.0f) * es_raw;

    float de = MODEL_DE_F_predict_one_f(x);
    float e_corrected = e_raw + de;

    float es_comp = saturation_vapor_pressure(T_comp);
    float RH_comp = CLAMP(100.0f * e_corrected / es_comp, 0.0f, 100.0f);

    float AH = 216.7f * e_corrected / (T_comp + 273.15f);

    float T_dew = 0.0f / 0.0f;
    if (e_corrected > 0.06f) {
        float gamma = logf(e_corrected / 6.112f);
        T_dew = 243.5f * gamma / (17.67f - gamma);
    }

    *out_temp = T_comp;
    *out_rh = RH_comp;
    *out_dew = T_dew;
    *out_ah = AH;
}

int main() {
    float T, RH, Td, AH;

    predict_coupled_f(
        33.4f, 37.7f, 526.0f, 439.0f, 3171.0f, 2307.0f,
        139.0f, 116.0f, 156.0f, 3937.0f,
        &T, &RH, &Td, &AH
    );

    printf("Temperature:        %.2f C  (expected: 27.93)\n", (double)T);
    printf("Relative Humidity:  %.2f %%  (expected: 52.23)\n", (double)RH);
    printf("Dew Point:          %.2f C  (expected: 17.25)\n", (double)Td);
    printf("Absolute Humidity:  %.2f g/m3 (expected: 14.16)\n", (double)AH);

    return 0;
}
