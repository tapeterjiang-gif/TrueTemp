/*
 * TrueTemp 温湿耦合补偿 — ESP32 实现
 * ============================================
 *
 * 只依赖 libm (<math.h>)，ESP-IDF 与 Arduino-ESP32 均可直接编译:
 *   ESP-IDF:  把 esp32/ 下三个文件加入 main 组件，自动编译 truetemp.c
 *   Arduino:  把 truetemp.h / truetemp.c / truetemp_model_data.h 放入 sketch 目录
 *
 * 模型数据是 static const，链接后位于 .rodata（flash），不消耗内部 RAM。
 * 全流程 float 运算，不触发 ESP32 的软件 double 模拟。
 */

#include <math.h>

#include "truetemp.h"
#include "truetemp_model_data.h"

#define TRUETEMP_CLAMP(x, lo, hi) ((x) < (lo) ? (lo) : (x) > (hi) ? (hi) : (x))

static float saturation_vapor_pressure(float t)
{
    return 6.112f * expf(17.67f * t / (t + 243.5f));
}

/* 树的左子节点恒为 node+1（先序布局），阈值/叶子值为 int16 量化，只读 flash */
static float predict_trees(const truetemp_node_t *nodes, const uint16_t *roots,
                           uint16_t num_trees, const float *x,
                           const float *feature_scale, float value_scale)
{
    int32_t sum = 0;

    for (uint16_t t = 0; t < num_trees; t++) {
        uint16_t node = roots[t];

        while (!nodes[node].is_leaf) {
            const truetemp_node_t *n = &nodes[node];
            float threshold = n->threshold_q * feature_scale[n->feature];
            node = (x[n->feature] <= threshold)
                       ? (uint16_t)(node + 1)
                       : n->right;
        }
        sum += nodes[node].value_q;
    }

    return (float)sum * value_scale;
}

void truetemp_compensate(const truetemp_input_t *in, truetemp_output_t *out)
{
    float x[TRUETEMP_FEATURE_COUNT];

    x[TRUETEMP_F_RAW_TEMPERATURE]      = in->raw_temperature;
    x[TRUETEMP_F_RAW_HUMIDITY]         = in->raw_humidity;
    x[TRUETEMP_F_TEMPERATURE_SOC]      = in->temperature_soc;
    x[TRUETEMP_F_TEMPERATURE_CO2]      = in->temperature_co2;
    x[TRUETEMP_F_TIME_UPTIME]          = in->time_uptime;
    x[TRUETEMP_F_SCREEN_BRIGHTNESS]    = in->screen_brightness;
    x[TRUETEMP_F_SCREEN_ON_DURATION_S] = in->screen_on_duration_s;
    x[TRUETEMP_F_CPU_LOAD_10S]         = in->cpu_load_10s;
    x[TRUETEMP_F_WIFI_LOAD_10S]        = in->wifi_load_10s;
    x[TRUETEMP_F_POWER_CHARGE_MA]      = in->power_charge_ma;
    x[TRUETEMP_F_C5_SOC_TEMP]          = in->c5_soc_temp;

    float t_comp = predict_trees(truetemp_temp_nodes, truetemp_temp_roots,
                                 TRUETEMP_TEMP_NUM_TREES, x,
                                 truetemp_feature_scale, TRUETEMP_TEMP_VALUE_SCALE);

    float e_raw = (in->raw_humidity / 100.0f) *
                  saturation_vapor_pressure(in->raw_temperature);

    float de = predict_trees(truetemp_de_nodes, truetemp_de_roots,
                             TRUETEMP_DE_NUM_TREES, x,
                             truetemp_feature_scale, TRUETEMP_DE_VALUE_SCALE);
    float e_corr = e_raw + de;

    float rh_comp = TRUETEMP_CLAMP(
        100.0f * e_corr / saturation_vapor_pressure(t_comp), 0.0f, 100.0f);

    float ah = 216.7f * e_corr / (t_comp + 273.15f);

    float t_dew = NAN;
    if (e_corr > 0.06f) {
        float gamma = logf(e_corr / 6.112f);
        t_dew = 243.5f * gamma / (17.67f - gamma);
    }

    out->temperature = t_comp;
    out->humidity = rh_comp;
    out->dew_point = t_dew;
    out->absolute_humidity = ah;
}
