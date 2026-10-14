import numpy as np
import pandas as pd
from .physics import vapor_pressure

INPUTS = ['raw_temperature', 'raw_humidity', 'temperature_soc', 'temperature_co2',
          'time_uptime', 'screen_brightness', 'screen_on_duration_s',
          'cpu_load_10s', 'wifi_load_10s', 'power_charge_ma']
DEFAULT = [x for x in INPUTS if x != 'temperature_soc'] + ['co2_temperature_delta']
UNITS = dict(zip(INPUTS, ['degC', '%RH', 'degC', 'degC', 's', 'level', 's', '%', '%', 'mA']))
UNITS.update(co2_temperature_delta='degC', e_raw_hpa='hPa')


def validate_records(frame):
    required = INPUTS + ['version', 'device_id']
    missing = set(required) - set(frame.columns)
    if missing or frame.empty:
        raise ValueError(f'DUT 输入为空或缺少列：{sorted(missing)}')
    frame = frame.copy()
    for key in INPUTS + ['version']:
        frame[key] = pd.to_numeric(frame[key], errors='raise')
        if not np.isfinite(frame[key]).all():
            raise ValueError(f'{key} 包含非有限数值')
    if (frame.version != 3).any():
        raise ValueError('只支持 DUT v3 协议')
    for key, low, high in [('raw_humidity', 0, 100), ('time_uptime', 0, 7200),
                            ('screen_brightness', 0, 255), ('screen_on_duration_s', 0, 3600),
                            ('cpu_load_10s', 0, 100), ('wifi_load_10s', 0, 100)]:
        if not frame[key].between(low, high).all():
            raise ValueError(f'{key} 超出协议范围')
    vapor_pressure(frame.raw_temperature, frame.raw_humidity)
    return frame


def build_features(frame, names):
    values = frame[INPUTS].copy()
    values['co2_temperature_delta'] = frame.temperature_co2 - frame.raw_temperature
    values['e_raw_hpa'] = vapor_pressure(frame.raw_temperature, frame.raw_humidity)
    if set(names) - set(values):
        raise ValueError('未知模型特征')
    return values[names]
