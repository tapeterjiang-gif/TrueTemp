"""
QingThermal 温湿耦合补偿 — 两阶段推理 (RK PX30 / Python 3)
=========================================================
Stage 1: LightGBM 预测补偿后温度 T_comp
Stage 2: LightGBM 预测水汽压残差 δe
          e_corrected = e_raw + δe
          所有输出统一从 e_corrected 和 T_comp 推导

依赖: Python 3 标准库 (仅 json, math)

输出:
  - Temperature (°C)           — 补偿后温度
  - Relative Humidity (%)      — 联动修正湿度
  - Dew Point (°C)             — 露点
  - Absolute Humidity (g/m³)   — 绝对湿度
"""

import json
import math
import os
import sys

_MODEL_DIR = os.path.dirname(os.path.abspath(__file__))

_mod_temp = None
_mod_de = None


def _load_model(path):
    with open(path, 'r') as f:
        return json.load(f)


def _predict_tree(x, trees):
    s = 0.0
    for tree in trees:
        node = tree
        while 'v' not in node:
            node = node['l'] if x[node['f']] <= node['t'] else node['r']
        s += node['v']
    return s


def predict(raw_temperature, raw_humidity, temperature_soc, temperature_co2,
            time_uptime, screen_on_duration, cpu_load_10s, ambient_lux,
            power_charge_ma, power_charge_mv):
    global _mod_temp, _mod_de

    if _mod_temp is None:
        _mod_temp = _load_model(os.path.join(_MODEL_DIR, 'models', 'model_temperature.json'))
    if _mod_de is None:
        _mod_de = _load_model(os.path.join(_MODEL_DIR, 'models', 'model_vapor_pressure.json'))

    x = [
        raw_temperature, raw_humidity, temperature_soc, temperature_co2,
        time_uptime, screen_on_duration, cpu_load_10s, ambient_lux,
        power_charge_ma, power_charge_mv,
    ]

    T_comp = _predict_tree(x, _mod_temp['trees'])

    es_raw = 6.112 * math.exp(17.67 * raw_temperature / (raw_temperature + 243.5))
    e_raw = (raw_humidity / 100.0) * es_raw

    de = _predict_tree(x, _mod_de['trees'])
    e_corrected = e_raw + de

    es_comp = 6.112 * math.exp(17.67 * T_comp / (T_comp + 243.5))
    RH_comp = max(0.0, min(100.0, 100.0 * e_corrected / es_comp))

    AH = 216.7 * e_corrected / (T_comp + 273.15)

    if e_corrected > 0.06:
        gamma = math.log(e_corrected / 6.112)
        T_dew = 243.5 * gamma / (17.67 - gamma)
    else:
        T_dew = float('nan')

    return T_comp, RH_comp, T_dew, AH


if __name__ == '__main__':
    if len(sys.argv) >= 11:
        args = [float(a) for a in sys.argv[1:11]]
    else:
        args = [33.4, 37.7, 526, 439, 3171, 2307, 139, 116, 156, 3937]

    T, RH, Td, AH = predict(*args)
    print(f'Temperature:        {T:.2f} °C')
    print(f'Relative Humidity:  {RH:.2f} %')
    print(f'Dew Point:          {Td:.2f} °C')
    print(f'Absolute Humidity:  {AH:.2f} g/m³')
