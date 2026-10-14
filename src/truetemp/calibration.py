"""Shared inference pipeline for validation, exams and CSV prediction."""
from pathlib import Path
import numpy as np
import pandas as pd
import lightgbm as lgb
from .artifacts import digest, read_json, write_csv_new
from .features import build_features, validate_records, UNITS
from .physics import (PHYSICS, vapor_pressure, relative_humidity, dew_point, absolute_humidity)


class Calibration:
    def __init__(self, metadata, models):
        self.metadata, self.models = metadata, models

    def predict(self, records):
        frame = validate_records(records)
        outputs = []
        for kind in ('temperature', 'vapor_pressure'):
            spec = self.metadata['models'][kind]
            if spec['constant'] is not None:
                outputs.append(np.full(len(frame), spec['constant']))
            else:
                outputs.append(self.models[kind].predict(build_features(frame, spec['features'])))
        corrected_t = frame.raw_temperature.to_numpy() + outputs[0]
        e_raw = vapor_pressure(frame.raw_temperature, frame.raw_humidity)
        candidate = e_raw + outputs[1]
        e_hat = np.maximum(0, candidate)
        humidity = relative_humidity(corrected_t, e_hat)
        frame['corrected_temperature'] = corrected_t
        frame['humidity_physical'] = relative_humidity(corrected_t, e_raw)
        frame['e_candidate_hpa'] = candidate
        frame['e_hat_hpa'] = e_hat
        frame['humidity_candidate'] = relative_humidity(corrected_t, candidate)
        frame['humidity_unclipped'] = humidity
        frame['corrected_humidity'] = np.clip(humidity, 0, 100)
        frame['dew_point'] = dew_point(e_hat)
        frame['absolute_humidity'] = absolute_humidity(e_hat, corrected_t)
        flags = [[] for _ in range(len(frame))]
        for key, limits in self.metadata['input_ranges'].items():
            for index in np.flatnonzero(~frame[key].between(*limits).to_numpy()):
                if 'out_of_training_range' not in flags[index]:
                    flags[index].append('out_of_training_range')
        for mask, label in ((candidate < 0, 'negative_vapor_pressure'), (humidity > 100, 'supersaturation')):
            for index in np.flatnonzero(mask):
                flags[index].append(label)
        frame['quality_flags'] = [';'.join(f) if f else 'ok' for f in flags]
        frame['model_id'] = self.metadata['model_id']
        return frame


def load_bundle(path):
    path = Path(path)
    meta = read_json(path / 'metadata.json')
    if meta.get('schema_version') != 1 or meta.get('protocol_version') != 3 or meta.get('physics') != PHYSICS:
        raise ValueError('模型 schema、协议或物理公式版本不支持')
    models = {}
    for kind in ('temperature', 'vapor_pressure'):
        spec = meta['models'][kind]
        if any(meta['units'].get(name) != UNITS.get(name) or name not in UNITS for name in spec['features']):
            raise ValueError('模型特征或单位不支持')
        if spec['constant'] is None:
            file = path / f'{kind}.txt'
            if digest(file) != spec['sha256']:
                raise ValueError('模型文件指纹不匹配')
            models[kind] = lgb.Booster(model_file=str(file))
            if models[kind].feature_name() != spec['features']:
                raise ValueError('模型特征顺序不一致')
    return Calibration(meta, models)


def display_predictions(frame):
    result = frame.copy()
    for key in ('corrected_temperature', 'humidity_physical', 'corrected_humidity',
                'humidity_unclipped', 'humidity_candidate', 'dew_point', 'absolute_humidity'):
        if key in result:
            result[key] = result[key].map(lambda v: f'{v:.1f}')
    return result


def predict_csv(source, model, output):
    frame = pd.read_csv(source)
    frame = load_bundle(model).predict(frame)
    frame['input_source'] = 'dut_csv'
    write_csv_new(display_predictions(frame), output)
    return len(frame)
