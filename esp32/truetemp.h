/*
 * TrueTemp 温湿耦合补偿 — ESP32 集成接口
 * ============================================
 *
 * 两阶段 LightGBM 补偿:
 *   Stage 1 预测补偿后温度 T_comp
 *   Stage 2 预测水汽压残差 de, e_corr = e_raw + de
 *   RH / 露点 / 绝对湿度全部由 e_corr 与 T_comp 推导
 *
 * 特征单位（与训练数据 DUT sheet 完全一致）:
 *   raw_temperature       °C
 *   raw_humidity          %RH
 *   temperature_soc       °C
 *   temperature_co2       °C
 *   time_uptime           秒
 *   screen_brightness     0~100
 *   screen_on_duration_s  秒
 *   cpu_load_10s          % (0~100)
 *   wifi_load_10s         % (0~100)
 *   power_charge_ma       mA (未充电为 0)
 *   c5_soc_temp           °C
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
 *
 * 模型数据默认 200 棵树/模型、约 87 KiB，const 数组放在 flash，不占 RAM。
 * 单次推理约 2000 次节点访问，1~10 秒调用一次即可。
 */

#ifndef TRUETEMP_H
#define TRUETEMP_H

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float raw_temperature;       /* °C */
    float raw_humidity;          /* %RH */
    float temperature_soc;       /* °C */
    float temperature_co2;       /* °C */
    float time_uptime;           /* s */
    float screen_brightness;     /* 0~100 */
    float screen_on_duration_s;  /* s */
    float cpu_load_10s;          /* % */
    float wifi_load_10s;         /* % */
    float power_charge_ma;       /* mA */
    float c5_soc_temp;           /* °C */
} truetemp_input_t;

typedef struct {
    float temperature;        /* 补偿后温度 °C */
    float humidity;           /* 补偿后相对湿度 % */
    float dew_point;          /* 露点 °C，e_corr <= 0.06 hPa 时为 NAN */
    float absolute_humidity;  /* 绝对湿度 g/m³ */
} truetemp_output_t;

/* 输入指针需非空并已填满 11 个字段 */
void truetemp_compensate(const truetemp_input_t *in, truetemp_output_t *out);

#ifdef __cplusplus
}
#endif

#endif
