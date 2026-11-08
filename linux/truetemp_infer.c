/*
 * TrueTemp 命令行推理 — Rockchip Linux 本机/交叉编译验证
 *
 * 用法:
 *   ./truetemp_infer 26.1 25.3 38.2 32.0 7200 80 0 11 3 0 30.9
 *   echo "26.1 25.3 38.2 32.0 7200 80 0 11 3 0 30.9" | ./truetemp_infer
 *
 * 11 个参数顺序与 truetemp_input_t 一致。
 */

#include <math.h>
#include <stdio.h>

#include "truetemp.h"

static void assign_input(truetemp_input_t *in, const float v[11])
{
    in->raw_temperature      = v[0];
    in->raw_humidity         = v[1];
    in->temperature_soc      = v[2];
    in->temperature_co2      = v[3];
    in->time_uptime          = v[4];
    in->screen_brightness    = v[5];
    in->screen_on_duration_s = v[6];
    in->cpu_load_10s         = v[7];
    in->wifi_load_10s        = v[8];
    in->power_charge_ma      = v[9];
    in->c5_soc_temp          = v[10];
}

static int read_input(truetemp_input_t *in)
{
    float v[11];

    if (scanf("%f %f %f %f %f %f %f %f %f %f %f",
              &v[0], &v[1], &v[2], &v[3], &v[4], &v[5],
              &v[6], &v[7], &v[8], &v[9], &v[10]) != 11) {
        return -1;
    }

    assign_input(in, v);
    return 0;
}

int main(int argc, char **argv)
{
    truetemp_input_t in;

    if (argc == 12) {
        float v[11];

        for (int i = 0; i < 11; i++) {
            if (sscanf(argv[i + 1], "%f", &v[i]) != 1) {
                fprintf(stderr, "invalid argument: %s\n", argv[i + 1]);
                return 2;
            }
        }
        assign_input(&in, v);
    } else if (read_input(&in) != 0) {
        fprintf(stderr,
                "usage: %s raw_t rh soc_t co2_t uptime brightness "
                "screen_on cpu wifi charge_ma c5_t\n"
                "   or: echo \"11 numbers\" | %s\n",
                argv[0], argv[0]);
        return 2;
    }

    truetemp_output_t out;
    truetemp_compensate(&in, &out);

    printf("temperature=%.4f humidity=%.4f dew_point=%.4f "
           "absolute_humidity=%.4f\n",
           out.temperature, out.humidity, out.dew_point, out.absolute_humidity);
    return 0;
}
