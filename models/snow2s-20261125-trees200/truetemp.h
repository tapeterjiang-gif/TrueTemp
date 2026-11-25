/*
 * TrueTemp 温湿耦合补偿 — Rockchip Linux 集成接口（PX30 / RK3506）
 * ================================================================
 *
 * Rockchip Linux 使用 float 树模型:
 *   - 模型保留全部 500 棵树，阈值/叶子值为 float，无 int16 量化
 *   - 模型数据约 332 KiB .rodata，适合内存与存储更宽裕的 Linux 设备
 *
 * 两阶段 LightGBM 补偿:
 *   Stage 1 预测补偿后温度 T_comp
 *   Stage 2 预测水汽压残差 de, e_corr = e_raw + de
 *   RH / 露点 / 绝对湿度全部由 e_corr 与 T_comp 推导
 *
 * 以下为既有 DUT v3 模型的单位:
 *   raw_temperature       °C
 *   raw_humidity          %RH
 *   temperature_soc       °C
 *   temperature_co2       °C
 *   time_uptime           秒
 *   screen_brightness     0~255（按 DUT 协议实际值）
 *   screen_on_duration_s  秒
 *   cpu_load_10s          % (0~100)
 *   wifi_load_10s         % (0~100)
 *   power_charge_ma       mA (未充电为 0)
 *   c5_soc_temp           °C
 *
 * 定义 TRUETEMP_SNOW2S 时使用独立的 DUT v4 模型和入口：
 * 背光为原始 0~1000，Wi-Fi 特征为 0~65535 Byte/s，无 C5 温度。
 * v4 的计时上限为 7200 秒，充电电流为电池侧 mA。
 * 带 delta-gate v1 策略的 Snow2S 模型在 raw_temperature 超过训练上限时，
 * 使用其余 9 项预测温差，再由 4 项热状态特征判断是否向训练温差中位数收缩。
 * 策略和阈值随模型头文件发布；调用方仍传入相同的 10 项字段。
 *
 * 用法:
 *   truetemp_input_t in = {
 *       .raw_temperature = 26.1f, .raw_humidity = 25.3f,
 *       .temperature_soc = 38.2f, .temperature_co2 = 32.0f,
 *       .time_uptime = 7200, .screen_brightness = 80,
 *       .screen_on_duration_s = 0, .cpu_load_10s = 11,
 *       .wifi_load_10s = 3, .power_charge_ma = 0, .c5_soc_temp = 30.9f,
 *   };
 *   truetemp_output_t out;
 *   truetemp_compensate(&in, &out);
 */

#ifndef TRUETEMP_LINUX_H
#define TRUETEMP_LINUX_H

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float raw_temperature;       /* °C */
    float raw_humidity;          /* %RH */
    float temperature_soc;       /* °C */
    float temperature_co2;       /* °C */
    float time_uptime;           /* s */
    float screen_brightness;     /* v3: 0~255, v4: 0~1000 */
    float screen_on_duration_s;  /* s */
    float cpu_load_10s;          /* % */
#ifdef TRUETEMP_SNOW2S
    float wifi_tx_bytes;         /* Byte/s */
    float power_charge_ma;       /* 电池侧 mA */
#else
    float wifi_load_10s;         /* % */
    float power_charge_ma;       /* mA */
    float c5_soc_temp;           /* °C */
#endif
} truetemp_input_t;

typedef struct {
    float temperature;        /* 补偿后温度 °C */
    float humidity;           /* 补偿后相对湿度 % */
    float dew_point;          /* 露点 °C，e_corr <= 0.06 hPa 时为 NAN */
    float absolute_humidity;  /* 绝对湿度 g/m³ */
} truetemp_output_t;

#ifdef TRUETEMP_SNOW2S
/* 输入指针需非空并已填满 10 个 v4 字段；delta-gate 模型遇到非有限输入时输出 NAN。 */
void truetemp_compensate_snow2s(const truetemp_input_t *in, truetemp_output_t *out);
#else
/* 输入指针需非空并已填满 11 个 v3 字段。 */
void truetemp_compensate(const truetemp_input_t *in, truetemp_output_t *out);
#endif

#ifdef __cplusplus
}
#endif

#endif
