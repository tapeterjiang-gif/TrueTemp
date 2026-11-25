import ctypes
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import lightgbm as lgb
import numpy as np
import pandas as pd

from gen_c_model import gen_linux_data
from linux.infer_truetemp import (
    DELTA_GATE_STRATEGY, SNOW2S_DELTA_FEATURES, SNOW2S_FEATURES, SNOW2S_HEAT_FEATURES,
    compensate, load_temperature_policy, load_trees,
)
from train_coupled_model import (
    lgb_params, load_and_merge, model_for_deploy, prepare_features, saturation_vapor_pressure,
)

PREVIOUS = Path('models/snow2s-20261114-r3-trees200')
HISTORICAL_PAIRS = PREVIOUS / 'training_pairs.csv'
NEW_FILE = Path('data/rk3506/20261125_1552.xlsx')
OUTPUT = Path('models/snow2s-20261125-trees200')
PAIR_WINDOW_MS = 5000
GAP_SECONDS = 60
PARAMETERS = dict(lgb_params(), n_estimators=200, n_jobs=2)
PREVIOUS_POLICY = json.loads((PREVIOUS / 'model_2stage_metadata.json').read_text())['temperature_policy']


def write_json(path, data, *, compact=False):
    options = {'separators': (',', ':')} if compact else {'indent': 2}
    path.write_text(json.dumps(data, allow_nan=False, **options) + '\n')


def train(frame):
    features, temperature, humidity, _ = prepare_features(frame, snow2s=True)
    delta = features.raw_temperature - temperature
    vapor_residual = (
        humidity * saturation_vapor_pressure(temperature)
        - features.raw_humidity * saturation_vapor_pressure(features.raw_temperature)
    ) / 100

    def fit(inputs, target):
        model = lgb.LGBMRegressor(**PARAMETERS).fit(inputs, target)
        return model_for_deploy(model, snow2s=True)

    temperature_model = fit(features, temperature)
    temperature_model['temperature_policy'] = {
        'strategy': DELTA_GATE_STRATEGY,
        'raw_temperature_max': float(features.raw_temperature.max()),
        'delta_median': float(delta.median()),
        'heat_gate_max': PREVIOUS_POLICY['heat_gate_max'],
        'shrink_fraction': PREVIOUS_POLICY['shrink_fraction'],
        'delta_model': fit(features[SNOW2S_DELTA_FEATURES], delta),
        'heat_model': fit(features[SNOW2S_HEAT_FEATURES], delta),
    }
    return temperature_model, fit(features, vapor_residual)


def input_rows(frame):
    return frame[SNOW2S_FEATURES].to_numpy(dtype=float).tolist()


def infer_comfort(rows, temperature, vapor, policy):
    return np.array([compensate(row, temperature, vapor, policy) for row in rows])


def metrics(truth, prediction):
    differences = prediction - truth
    absolute = np.abs(differences)
    return {
        name: {'mae': float(absolute[:, i].mean()),
               'rmse': float(np.sqrt(np.mean(differences[:, i] ** 2))),
               'bias': float(differences[:, i].mean()),
               'p95_abs_error': float(np.quantile(absolute[:, i], .95))}
        for i, name in enumerate(('temperature', 'humidity'))
    }


def numeric_ranges(frame, columns):
    return {name: [float(frame[name].min()), float(frame[name].max())] for name in columns}


def basic_policy(policy):
    return {name: value for name, value in policy.items() if not name.endswith('_model')}


def validate_c(candidate, build, combined, new):
    library = build / 'libtruetemp.so'
    subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-std=c99', '-Wall', '-Wextra', '-Werror',
                    '-DTRUETEMP_SNOW2S', str(candidate / 'truetemp.c'), '-lm', '-o', str(library)],
                   check=True)
    runtime = ctypes.CDLL(str(library.resolve()))
    infer = runtime.truetemp_compensate_snow2s
    inputs, outputs = ctypes.c_float * 10, ctypes.c_float * 4
    infer.argtypes = [ctypes.POINTER(inputs), ctypes.POINTER(outputs)]
    infer.restype = None
    output = outputs()

    def native(row):
        infer(ctypes.byref(inputs(*row)), ctypes.byref(output))
        return list(output)

    temperature_path = candidate / 'model_temperature.json'
    temperature = load_trees(temperature_path, 200, snow2s=True)
    vapor = load_trees(candidate / 'model_vapor_pressure.json', 200, snow2s=True)
    policy = load_temperature_policy(temperature_path, 200, snow2s=True)
    cap = np.float32(policy['raw_temperature_max'])
    above_cap = float(np.nextafter(cap, np.float32(np.inf)))
    new_rows = input_rows(new)
    boundary = [[float(cap), *row[1:]] for row in new_rows]
    outside = [[above_cap, *row[1:]] for row in new_rows]
    old_inputs = input_rows(combined)
    rows = np.array(old_inputs + boundary + outside, dtype=np.float32).tolist()

    for name in ('raw_temperature_max', 'delta_median', 'heat_gate_max', 'shrink_fraction'):
        policy[name] = float(np.float32(policy[name]))
    reference = infer_comfort(rows, temperature, vapor, policy)
    native_prediction = np.array([native(row) for row in rows])
    difference = np.abs(native_prediction - reference)
    if not np.isfinite(difference).all() or np.max(difference) > 0.001:
        raise RuntimeError(f'C/Python inference mismatch: {difference.max(axis=0)}')

    actual_python = infer_comfort(old_inputs, temperature, vapor,
                                  load_temperature_policy(temperature_path, 200, snow2s=True))
    deployment_difference = np.abs(native_prediction[:len(combined)] - actual_python)
    boundary_change = np.abs(native_prediction[-len(new):, 0] - native_prediction[len(combined):-len(new), 0])
    names = ('temperature', 'humidity', 'dew_point', 'absolute_humidity')
    return {
        'host_compile': True,
        'rows': len(rows),
        'comparison': 'Python uses float32 inputs and policy constants for the synthetic boundary probes and aligned comparison.',
        'max_abs_c_python_aligned': dict(zip(names, difference.max(axis=0).tolist())),
        'max_abs_c_python_original_inputs': dict(zip(names, deployment_difference.max(axis=0).tolist())),
        'boundary_probes': len(boundary),
        'boundary_temperature_jump_max': float(boundary_change.max()),
        'model_header_bytes': (candidate / 'truetemp_snow2s_model_data.h').stat().st_size,
        'tree_count': len(temperature) + len(vapor) + sum(
            len(policy[f'{name}_model']['trees']) for name in ('delta', 'heat')
        ),
    }


def main():
    if OUTPUT.exists():
        raise FileExistsError(OUTPUT)
    previous = pd.read_csv(HISTORICAL_PAIRS)
    previous['source_file'] = previous.source_file.map(lambda s: str(NEW_FILE.parent / Path(s).name))
    new = load_and_merge([NEW_FILE], snow2s=True, report=True, pair_window_ms=PAIR_WINDOW_MS)
    new = new[previous.columns]
    combined = pd.concat([previous, new], ignore_index=True)
    times = pd.to_datetime(combined.timestamp, format='mixed', utc=True).dt.tz_convert('Asia/Shanghai')
    if not times.is_monotonic_increasing or combined.duplicated(['source_file', 'timestamp', 'received_at']).any():
        raise ValueError('训练配对有重复或时间排序错误')
    if not all(Path(name).is_file() for name in combined.source_file.unique()):
        raise FileNotFoundError('配对表包含缺失的工作簿')

    holdout = new.tail(math.ceil(len(new) / 5)).copy()
    holdout_start = times.iloc[len(combined) - len(holdout)]
    before_gap = times < holdout_start - pd.Timedelta(seconds=GAP_SECONDS)
    train_frame = combined.loc[before_gap]
    print(f'Training evaluation model on {len(train_frame)} pairs, holdout {len(holdout)}', flush=True)
    evaluation_temperature, evaluation_vapor = train(train_frame)
    previous_temperature = PREVIOUS / 'model_temperature.json'
    rows = input_rows(holdout)
    baseline = infer_comfort(
        rows, load_trees(previous_temperature, 200, snow2s=True),
        load_trees(PREVIOUS / 'model_vapor_pressure.json', 200, snow2s=True),
        load_temperature_policy(previous_temperature, 200, snow2s=True),
    )[:, :2]
    evaluation_policy = evaluation_temperature['temperature_policy']
    predicted = infer_comfort(rows, evaluation_temperature['trees'], evaluation_vapor['trees'],
                              evaluation_policy)[:, :2]
    truth = holdout[['temperature', 'humidity']].to_numpy(dtype=float)
    evaluation = {
        'previous_model': str(PREVIOUS), 'historical_pairs': len(previous), 'new_pairs': len(new),
        'train_pairs': len(train_frame), 'holdout_pairs': len(holdout),
        'excluded_gap_pairs': len(combined) - len(train_frame) - len(holdout),
        'holdout_start': holdout_start.isoformat(), 'holdout_end': times.iloc[-1].isoformat(),
        'gap_seconds': GAP_SECONDS,
        'new_reference_range': numeric_ranges(new, ('temperature', 'humidity')),
        'new_input_range': numeric_ranges(new, ('raw_temperature', 'raw_humidity')),
        'holdout_reference_range': numeric_ranges(holdout, ('temperature', 'humidity')),
        'holdout_previous_outside_temperature_samples': int(
            (holdout.raw_temperature > PREVIOUS_POLICY['raw_temperature_max']).sum()),
        'holdout_candidate_outside_temperature_samples': int(
            (holdout.raw_temperature > evaluation_policy['raw_temperature_max']).sum()),
        'holdout_raw': metrics(truth, np.array(rows)[:, :2]),
        'holdout_previous': metrics(truth, baseline), 'holdout_candidate': metrics(truth, predicted),
    }
    for i, name in enumerate(('temperature', 'humidity')):
        holdout[f'previous_{name}'] = baseline[:, i]
        holdout[f'candidate_{name}'] = predicted[:, i]

    print(f'Training final model on {len(combined)} pairs', flush=True)
    temperature, vapor = train(combined)
    metadata = {
        'protocol_version': 4, 'samples': len(combined), 'features': SNOW2S_FEATURES,
        'training_parameters': PARAMETERS, 'raw_pair_window_seconds': PAIR_WINDOW_MS / 1000,
        'temperature_policy': basic_policy(temperature['temperature_policy']),
        'input_ranges': numeric_ranges(combined, SNOW2S_FEATURES),
        'reference_ranges': numeric_ranges(combined, ('temperature', 'humidity')),
        'source_sha256': {str(path): hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
                          for path in (HISTORICAL_PAIRS, NEW_FILE)},
        'training_recipe': str(Path(__file__).resolve().relative_to(Path.cwd())),
        'versions': {'python': sys.version.split()[0], 'lightgbm': lgb.__version__,
                     'numpy': np.__version__, 'pandas': pd.__version__},
        'validation_scope': 'The final model was refitted on all pairs after a within-session chronological holdout.',
    }
    with tempfile.TemporaryDirectory(prefix='.snow2s-20261125-', dir=OUTPUT.parent) as tmp:
        build = Path(tmp)
        candidate = build / 'candidate'
        candidate.mkdir()
        write_json(candidate / 'model_temperature.json', temperature, compact=True)
        write_json(candidate / 'model_vapor_pressure.json', vapor, compact=True)
        write_json(candidate / 'model_2stage_metadata.json', metadata)
        write_json(candidate / 'evaluation.json', evaluation)
        combined.to_csv(candidate / 'training_pairs.csv', index=False)
        combined[previous.columns[:5]].to_csv(candidate / 'pairs.csv', index=False)
        holdout.to_csv(candidate / 'holdout_predictions.csv', index=False)
        gen_linux_data(str(candidate / 'model_temperature.json'), str(candidate / 'model_vapor_pressure.json'),
                       str(candidate / 'truetemp_snow2s_model_data.h'), max_trees=200, snow2s=True)
        for name in ('truetemp.c', 'truetemp.h', 'truetemp_infer.c', 'infer_truetemp.py'):
            shutil.copyfile(Path('linux') / name, candidate / name)
        write_json(candidate / 'validation.json', validate_c(candidate, build, combined, new))
        candidate.rename(OUTPUT)
    print(json.dumps(evaluation, indent=2), flush=True)


if __name__ == '__main__':
    main()
