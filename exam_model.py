"""
对 50D6_Data_241702.xlsx 进行模型考试（评估）
使用 JSON 格式的嵌套树模型推理，不依赖 lightgbm 运行时库
"""

import warnings
warnings.filterwarnings('ignore')

import json
import math
import os
import pandas as pd
import numpy as np
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score

BASE = os.path.dirname(os.path.abspath(__file__))


def saturation_vapor_pressure(T):
    return 6.112 * np.exp(17.67 * T / (T + 243.5))


def _predict_tree(x, trees):
    s = 0.0
    for tree in trees:
        node = tree
        while 'v' not in node:
            node = node['l'] if x[node['f']] <= node['t'] else node['r']
        s += node['v']
    return s


class CoupledPredictor:
    def __init__(self):
        with open(os.path.join(BASE, 'models', 'model_temperature.json'), 'r') as f:
            self._mod_temp = json.load(f)
        with open(os.path.join(BASE, 'models', 'model_vapor_pressure.json'), 'r') as f:
            self._mod_de = json.load(f)
        self._features = self._mod_temp['features']

    def predict_batch(self, X):
        """
        X: numpy array or DataFrame with columns matching self._features
        Returns: T_comp, RH_comp, T_dew, AH, e_raw, e_corrected, de_pred
        """
        n = len(X)
        T_comp = np.zeros(n)
        de_pred = np.zeros(n)

        temp_trees = self._mod_temp['trees']
        de_trees = self._mod_de['trees']

        for i in range(n):
            x = X.iloc[i].tolist() if hasattr(X, 'iloc') else X[i].tolist()
            T_comp[i] = _predict_tree(x, temp_trees)
            de_pred[i] = _predict_tree(x, de_trees)

        raw_temp_idx = self._features.index('raw_temperature')
        raw_humid_idx = self._features.index('raw_humidity')
        T_raw = X.iloc[:, raw_temp_idx].values if hasattr(X, 'iloc') else X[:, raw_temp_idx]
        RH_raw = X.iloc[:, raw_humid_idx].values if hasattr(X, 'iloc') else X[:, raw_humid_idx]

        e_raw = (RH_raw / 100.0) * saturation_vapor_pressure(T_raw)
        e_corrected = e_raw + de_pred

        es_comp = saturation_vapor_pressure(T_comp)
        RH_comp = np.clip(100.0 * e_corrected / es_comp, 0.0, 100.0)
        AH = 216.7 * e_corrected / (T_comp + 273.15)

        with np.errstate(divide='ignore', invalid='ignore'):
            gamma = np.log(e_corrected / 6.112)
        T_dew = np.where(e_corrected > 0.06, 243.5 * gamma / (17.67 - gamma), np.nan)

        return T_comp, RH_comp, T_dew, AH, e_raw, e_corrected, de_pred


def load_test_data(filepath):
    ref = pd.read_excel(filepath, sheet_name='REF-50D6')
    dut = pd.read_excel(filepath, sheet_name='2D58')
    ref['timestamp'] = pd.to_datetime(ref['timestamp'])
    dut['timestamp'] = pd.to_datetime(dut['timestamp'])
    dut = dut.dropna(subset=['timestamp'])
    m = pd.merge(ref, dut, on='timestamp', how='inner')
    print(f'  Loaded: {len(ref)} REF rows, {len(dut)} DUT rows, {len(m)} merged')
    return m


def prepare_features(df):
    drop_cols = ['screen_brightness', 'wifi_rssi', 'wifi_tx_bytes']
    df = df.drop(columns=[c for c in drop_cols if c in df.columns])
    feature_cols = [
        'raw_temperature', 'raw_humidity', 'temperature_soc', 'temperature_co2',
        'time_uptime', 'screen_on_duration', 'cpu_load_10s', 'ambient_lux',
        'power_charge_ma', 'power_charge_mv',
    ]
    X = df[feature_cols].copy()
    y_temp = df['temperature'].copy()
    y_humid = df['humidity'].copy()
    return X, y_temp, y_humid, feature_cols


def print_metric(y_true, y_pred, name, unit=''):
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    mae = mean_absolute_error(y_true, y_pred)
    r2 = r2_score(y_true, y_pred)
    err = np.abs(y_true - y_pred)
    p50 = np.percentile(err, 50)
    p95 = np.percentile(err, 95)
    p99 = np.percentile(err, 99)
    max_err = np.max(err)
    print(f'\n  {"—"*50}')
    print(f'  {name}:')
    print(f'    RMSE  = {rmse:.4f}{unit}')
    print(f'    MAE   = {mae:.4f}{unit}')
    print(f'    R²    = {r2:.4f}')
    print(f'    P50   = {p50:.4f}{unit}')
    print(f'    P95   = {p95:.4f}{unit}')
    print(f'    P99   = {p99:.4f}{unit}')
    print(f'    Max   = {max_err:.4f}{unit}')
    return rmse, mae, r2


def print_summary_table(y_temp_true, T_comp, y_humid_true, RH_comp, T_raw, RH_raw, T_dew, AH):
    print(f'\n{"="*110}')
    print('逐样本对比')
    print(f'{"="*110}')
    hdr = (f'{"#":>4s} {"RawT":>6s} {"RawH":>5s} '
           f'{"TrueT":>6s} {"CompT":>6s} {"ΔT":>6s} '
           f'{"TrueH":>6s} {"CompH":>6s} {"ΔH":>6s} '
           f'{"Dew":>6s} {"AH":>7s}')
    print(hdr)
    print('-' * 110)

    indices = list(range(15)) + list(range(len(y_temp_true) - 5, len(y_temp_true)))
    for i in indices:
        dt = y_temp_true.iloc[i] - T_comp[i]
        dh = y_humid_true.iloc[i] - RH_comp[i]
        print(f'{i:4d} {T_raw[i]:6.2f} {RH_raw[i]:5.2f} '
              f'{y_temp_true.iloc[i]:6.2f} {T_comp[i]:6.2f} {dt:+6.2f} '
              f'{y_humid_true.iloc[i]:6.2f} {RH_comp[i]:6.2f} {dh:+6.2f} '
              f'{T_dew[i]:6.2f} {AH[i]:7.3f}')


def main():
    test_file = os.path.join(BASE, 'data', '50D6_Data_241702.xlsx')

    print('=' * 60)
    print('模型考试：50D6_Data_241702.xlsx')
    print('=' * 60)

    print('\n[1] 加载测试数据...')
    df = load_test_data(test_file)

    print('\n[2] 准备特征...')
    X, y_temp, y_humid, feature_cols = prepare_features(df)
    print(f'  样本数: {len(X)}')
    print(f'  特征数: {len(feature_cols)}')

    print('\n[3] 加载模型 (JSON 嵌套树)...')
    pred = CoupledPredictor()
    print(f'  温度模型: {len(pred._mod_temp["trees"])} trees')
    print(f'  水汽压模型: {len(pred._mod_de["trees"])} trees')

    print('\n[4] 执行推理...')
    T_comp, RH_comp, T_dew, AH, e_raw, e_corrected, de_pred = pred.predict_batch(X)

    T_raw = X['raw_temperature'].values
    RH_raw = X['raw_humidity'].values

    print(f'\n{"="*60}')
    print('考试结果')
    print(f'{"="*60}')

    print(f'\n  [Baseline — 原始传感器 vs 参考真值]')
    print_metric(y_temp, T_raw, '原始温度误差', '°C')
    print_metric(y_humid, RH_raw, '原始湿度误差', '%')

    print(f'\n  [补偿后 vs 参考真值]')
    t_rmse, t_mae, t_r2 = print_metric(y_temp, T_comp, '补偿后温度误差', '°C')
    h_rmse, h_mae, h_r2 = print_metric(y_humid, RH_comp, '补偿后湿度误差', '%')

    print(f'\n  [改善幅度]')
    base_t_rmse = np.sqrt(mean_squared_error(y_temp, T_raw))
    base_h_rmse = np.sqrt(mean_squared_error(y_humid, RH_raw))
    print(f'    温度 RMSE: {base_t_rmse:.4f}°C → {t_rmse:.4f}°C (改善 {100*(1-t_rmse/max(base_t_rmse,0.001)):.1f}%)')
    print(f'    湿度 RMSE: {base_h_rmse:.4f}%  → {h_rmse:.4f}%  (改善 {100*(1-h_rmse/max(base_h_rmse,0.001)):.1f}%)')

    print(f'\n  [中间量]')
    print(f'    水汽压修正均值: {de_pred.mean():.3f} hPa')
    print(f'    e_raw 均值:      {e_raw.mean():.3f} hPa')
    print(f'    e_cor 均值:      {e_corrected.mean():.3f} hPa')
    print(f'    露点均值:        {np.nanmean(T_dew):.2f}°C')
    print(f'    绝对湿度均值:    {np.mean(AH):.2f} g/m³')

    print_summary_table(y_temp, T_comp, y_humid, RH_comp, T_raw, RH_raw, T_dew, AH)

    print(f'\n{"="*60}')
    print('考试总结')
    print(f'{"="*60}')
    print(f'  温度: RMSE={t_rmse:.4f}°C, MAE={t_mae:.4f}°C, R²={t_r2:.4f}')
    print(f'  湿度: RMSE={h_rmse:.4f}%,  MAE={h_mae:.4f}%,  R²={h_r2:.4f}')
    print()


if __name__ == '__main__':
    main()
