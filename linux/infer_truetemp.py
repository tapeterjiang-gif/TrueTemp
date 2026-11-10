#!/usr/bin/env python3
"""TrueTemp Linux 纯标准库推理（PX30 / RK3506）

不依赖 numpy / lightgbm，只读取 models/*.json 嵌套树模型。
适合已经在设备上部署 Python 3 的场景；对性能敏感时改用 truetemp.c。

用法:
    python3 infer_truetemp.py 26.1 25.3 38.2 32.0 7200 80 0 11 3 0 30.9
    echo "26.1 25.3 38.2 32.0 7200 80 0 11 3 0 30.9" | python3 infer_truetemp.py
    python3 infer_truetemp.py --trees 200 26.1 25.3 38.2 32.0 7200 80 0 11 3 0 30.9
"""

import argparse
import json
import math
import os
import sys

FEATURES = [
    'raw_temperature', 'raw_humidity', 'temperature_soc', 'temperature_co2',
    'time_uptime', 'screen_brightness', 'screen_on_duration_s', 'cpu_load_10s',
    'wifi_load_10s', 'power_charge_ma', 'c5_soc_temp',
]
SNOW2S_FEATURES = [
    'raw_temperature', 'raw_humidity', 'temperature_soc', 'temperature_co2',
    'time_uptime', 'screen_brightness', 'screen_on_duration_s', 'cpu_load_10s',
    'wifi_tx_bytes', 'power_charge_ma',
]


def load_trees(path, max_trees, snow2s=False):
    with open(path, 'r') as f:
        model = json.load(f)
    version = 4 if snow2s else 3
    features = SNOW2S_FEATURES if snow2s else FEATURES
    if model.get('protocol_version', 3) != version or model['features'] != features:
        raise ValueError(f'{path}: model protocol or feature order does not match DUT v{version}')
    if not model['trees'] or max_trees <= 0:
        raise ValueError(f'{path}: no trees selected')
    return model['trees'][:max_trees]


def predict(trees, x):
    total = 0.0
    for tree in trees:
        node = tree
        while 'v' not in node:
            node = node['l'] if x[node['f']] <= node['t'] else node['r']
        total += node['v']
    return total


def saturation_vapor_pressure(t):
    return 6.112 * math.exp(17.67 * t / (t + 243.5))


def compensate(values, temp_trees, de_trees):
    t_raw, rh_raw = values[0], values[1]
    x = list(values)

    t_comp = predict(temp_trees, x)
    e_raw = (rh_raw / 100.0) * saturation_vapor_pressure(t_raw)
    e_corr = e_raw + predict(de_trees, x)

    es_comp = saturation_vapor_pressure(t_comp)
    rh_comp = min(100.0, max(0.0, 100.0 * e_corr / es_comp))
    ah = 216.7 * e_corr / (t_comp + 273.15)

    dew = float('nan')
    if e_corr > 0.06:
        gamma = math.log(e_corr / 6.112)
        dew = 243.5 * gamma / (17.67 - gamma)

    return t_comp, rh_comp, dew, ah


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default_models = os.path.join(here, '..', 'models')

    parser = argparse.ArgumentParser(description='TrueTemp Linux inference (stdlib only)')
    parser.add_argument('values', nargs='*', type=float,
                        help='inputs in model feature order (see README)')
    parser.add_argument('--snow2s', action='store_true', help='use the Snow2S DUT v4 Rockchip model')
    parser.add_argument('--model-dir',
                        help='directory containing model_temperature.json / model_vapor_pressure.json')
    parser.add_argument('--trees', type=int, default=500,
                        help='use only the first N trees per model (default: 500)')
    args = parser.parse_args()

    if args.values:
        values = args.values
    else:
        line = sys.stdin.readline()
        values = [float(v) for v in line.split()]

    features = SNOW2S_FEATURES if args.snow2s else FEATURES
    if len(values) != len(features):
        parser.error(f'expected {len(features)} values: {", ".join(features)}')
    if args.snow2s and (not all(map(math.isfinite, values)) or not 0 <= values[5] <= 1000 or
                        not 0 <= values[8] <= 65535):
        parser.error('Snow2S expects finite inputs, brightness 0..1000 and Wi-Fi TX 0..65535 B/s')

    model_dir = args.model_dir or (os.path.join(default_models, 'snow2s') if args.snow2s else default_models)
    try:
        temp_trees = load_trees(os.path.join(model_dir, 'model_temperature.json'), args.trees, args.snow2s)
        de_trees = load_trees(os.path.join(model_dir, 'model_vapor_pressure.json'), args.trees, args.snow2s)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    t_comp, rh_comp, dew, ah = compensate(values, temp_trees, de_trees)
    print(f'temperature={t_comp:.4f} humidity={rh_comp:.4f} '
          f'dew_point={dew:.4f} absolute_humidity={ah:.4f}')


if __name__ == '__main__':
    main()
