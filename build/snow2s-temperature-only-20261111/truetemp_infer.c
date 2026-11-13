/*
 * TrueTemp 命令行推理 — Rockchip Linux 本机/交叉编译验证
 *
 * 用法:
 *   ./truetemp_infer 26.1 25.3 38.2 32.0 7200 80 0 11 3 0 30.9
 *   echo "26.1 25.3 38.2 32.0 7200 80 0 11 3 0 30.9" | ./truetemp_infer
 *
 * 参数顺序与所选模型的 truetemp_input_t 一致。
 */

#include <math.h>
#include <stdio.h>

#include "truetemp.h"

#ifdef TRUETEMP_SNOW2S
enum { INPUT_COUNT = 10 };
#else
enum { INPUT_COUNT = 11 };
#endif

static void assign_input(truetemp_input_t *in, const float v[INPUT_COUNT])
{
    in->raw_temperature      = v[0];
    in->raw_humidity         = v[1];
    in->temperature_soc      = v[2];
    in->temperature_co2      = v[3];
    in->time_uptime          = v[4];
    in->screen_brightness    = v[5];
    in->screen_on_duration_s = v[6];
    in->cpu_load_10s         = v[7];
    in->power_charge_ma      = v[9];
#ifdef TRUETEMP_SNOW2S
    in->wifi_tx_bytes        = v[8];
#else
    in->wifi_load_10s        = v[8];
    in->c5_soc_temp          = v[10];
#endif
}

static int read_input(int argc, char **argv, float values[INPUT_COUNT])
{
    if (argc != 1 && argc != INPUT_COUNT + 1)
        return -1;
    for (int i = 0; i < INPUT_COUNT; i++) {
        char extra;
        const int count = argc == 1 ? scanf("%f", &values[i])
                                    : sscanf(argv[i + 1], "%f %c", &values[i], &extra);
        if (count != 1 || !isfinite(values[i]))
            return -1;
    }
#ifdef TRUETEMP_SNOW2S
    if (values[5] < 0 || values[5] > 1000 || values[8] < 0 || values[8] > 65535)
        return -1;
#endif
    return 0;
}

int main(int argc, char **argv)
{
    float values[INPUT_COUNT];
    if (read_input(argc, argv, values) != 0) {
        fprintf(stderr,
                "usage: %s raw_t rh soc_t co2_t uptime brightness "
#ifdef TRUETEMP_SNOW2S
                "screen_on cpu wifi_tx_bytes charge_ma\n"
                "       brightness 0..1000, wifi_tx_bytes 0..65535 B/s\n"
#else
                "screen_on cpu wifi charge_ma c5_t\n"
#endif
                "   or: echo \"%d numbers\" | %s\n",
                argv[0], INPUT_COUNT, argv[0]);
        return 2;
    }

    truetemp_input_t in;
    assign_input(&in, values);
    truetemp_output_t out;
#ifdef TRUETEMP_SNOW2S
    truetemp_compensate_snow2s(&in, &out);
#else
    truetemp_compensate(&in, &out);
#endif

    printf("temperature=%.4f humidity=%.4f dew_point=%.4f "
           "absolute_humidity=%.4f\n",
           out.temperature, out.humidity, out.dew_point, out.absolute_humidity);
    return 0;
}
