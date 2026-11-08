/*
 * TrueTemp 温湿耦合补偿 — Rockchip Linux 实现（PX30 / RK3506）
 * ==========================================================
 *
 * 只依赖 libm。模型数据由 gen_c_model.py --linux 生成:
 *   python3 gen_c_model.py --linux
 *
 * 全流程 float 运算，模型数组为 static const，位于只读段。
 */

#include <math.h>

#include "truetemp.h"
#include "truetemp_model_data.h"

#define TRUETEMP_CLAMP(x, lo, hi) ((x) < (lo) ? (lo) : (x) > (hi) ? (hi) : (x))

static float saturation_vapor_pressure(float t)
{
    return 6.112f * expf(17.67f * t / (t + 243.5f));
}

/* 树的左子节点恒为 node+1（先序布局），只存右子节点索引 */
static float predict_trees(const truetemp_node_t *nodes, const uint16_t *roots,
                           uint16_t num_trees, const float *x)
{
    float sum = 0.0f;

    for (uint16_t t = 0; t < num_trees; t++) {
        uint16_t node = roots[t];

        while (!nodes[node].is_leaf) {
            const truetemp_node_t *n = &nodes[node];
            node = (x[n->feature] <= n->threshold)
                       ? (uint16_t)(node + 1)
                       : n->right;
        }
        sum += nodes[node].value;
    }

    return sum;
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
                                 TRUETEMP_TEMP_NUM_TREES, x);

    float e_raw = (in->raw_humidity / 100.0f) *
                  saturation_vapor_pressure(in->raw_temperature);

    float de = predict_trees(truetemp_de_nodes, truetemp_de_roots,
                             TRUETEMP_DE_NUM_TREES, x);
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
