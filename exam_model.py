"""
对新采集的数据文件进行模型考试（评估）
使用 JSON 嵌套树模型推理，不依赖 lightgbm 运行时库

用法:
    python3 exam_model.py data/20261023_1948.xlsx
    python3 exam_model.py data/20261023_1948.xlsx --trees 200   # 模拟 ESP32 部署模型
"""

import warnings
warnings.filterwarnings('ignore')

import glob
import json
import os
import sys

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
    def __init__(self, max_trees=None):
        with open(os.path.join(BASE, 'models', 'model_temperature.json'), 'r') as f:
            self._mod_temp = json.load(f)
        with open(os.path.join(BASE, 'models', 'model_vapor_pressure.json'), 'r') as f:
            self._mod_de = json.load(f)
        self._features = self._mod_temp['features']
        if self._mod_de['features'] != self._features:
            raise SystemExit('两个模型的特征顺序不一致')

        self._temp_trees = self._mod_temp['trees'][:max_trees]
        self._de_trees = self._mod_de['trees'][:max_trees]

    def predict_batch(self, X):
        """
        X: 含模型特征列的 DataFrame
        Returns: T_comp, RH_comp, T_dew, AH, e_raw, e_corrected, de_pred
        """
        x_rows = X[self._features].astype(float).values
        n = len(x_rows)
        T_comp = np.zeros(n)
        de_pred = np.zeros(n)

        for i in range(n):
            x = x_rows[i]
            T_comp[i] = _predict_tree(x, self._temp_trees)
            de_pred[i] = _predict_tree(x, self._de_trees)

        T_raw = X['raw_temperature'].values
        RH_raw = X['raw_humidity'].values

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
    ref = pd.read_excel(filepath, sheet_name='REF')
    dut = pd.read_excel(filepath, sheet_name='DUT')
    ref['ts'] = pd.to_datetime(ref['timestamp'])
    dut['ts'] = pd.to_datetime(dut['received_at'])
    m = pd.merge(ref, dut, on='ts', how='inner')
    print(f'  Loaded: {len(ref)} REF rows, {len(dut)} DUT rows, {len(m)} merged')
    return m


def print_metric(y_true, y_pred, name, unit=''):
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    mae = mean_absolute_error(y_true, y_pred)
    r2 = r2_score(y_true, y_pred)
    err = np.abs(y_true - y_pred)
    print(f'\n  {"—"*50}')
    print(f'  {name}:')
    print(f'    RMSE  = {rmse:.4f}{unit}')
    print(f'    MAE   = {mae:.4f}{unit}')
    print(f'    R²    = {r2:.4f}')
    print(f'    P50   = {np.percentile(err, 50):.4f}{unit}')
    print(f'    P95   = {np.percentile(err, 95):.4f}{unit}')
    print(f'    Max   = {np.max(err):.4f}{unit}')
    return rmse, mae, r2


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    max_trees = None
    if '--trees' in sys.argv:
        max_trees = int(sys.argv[sys.argv.index('--trees') + 1])

    if args:
        test_file = args[0]
    else:
        candidates = sorted(glob.glob(os.path.join(BASE, 'data', '*.xlsx')))
        if not candidates:
            raise SystemExit('data/ 下没有 Excel 文件')
        test_file = candidates[-1]

    print('=' * 60)
    print(f'模型考试：{os.path.basename(test_file)}')
    if max_trees:
        print(f'树数量：前 {max_trees} 棵/模型（模拟 ESP32 部署模型）')
    print('=' * 60)

    print('\n[1] 加载测试数据...')
    df = load_test_data(test_file)

    print('\n[2] 加载模型 (JSON 嵌套树)...')
    pred = CoupledPredictor(max_trees=max_trees)
    print(f'  温度模型: {len(pred._temp_trees)} trees')
    print(f'  水汽压模型: {len(pred._de_trees)} trees')
    print(f'  特征数: {len(pred._features)}')

    print('\n[3] 执行推理...')
    T_comp, RH_comp, T_dew, AH, e_raw, e_corrected, de_pred = pred.predict_batch(df)

    T_raw = df['raw_temperature'].values
    RH_raw = df['raw_humidity'].values

    print(f'\n{"="*60}')
    print('考试结果')
    print(f'{"="*60}')

    print(f'\n  [Baseline — 原始传感器 vs 参考真值]')
    print_metric(df['temperature'], T_raw, '原始温度误差', '°C')
    print_metric(df['humidity'], RH_raw, '原始湿度误差', '%')

    print(f'\n  [补偿后 vs 参考真值]')
    t_rmse, t_mae, t_r2 = print_metric(df['temperature'], T_comp, '补偿后温度误差', '°C')
    h_rmse, h_mae, h_r2 = print_metric(df['humidity'], RH_comp, '补偿后湿度误差', '%')

    print(f'\n  [改善幅度]')
    base_t_rmse = np.sqrt(mean_squared_error(df['temperature'], T_raw))
    base_h_rmse = np.sqrt(mean_squared_error(df['humidity'], RH_raw))
    print(f'    温度 RMSE: {base_t_rmse:.4f}°C → {t_rmse:.4f}°C (改善 {100*(1-t_rmse/max(base_t_rmse,0.001)):.1f}%)')
    print(f'    湿度 RMSE: {base_h_rmse:.4f}%  → {h_rmse:.4f}%  (改善 {100*(1-h_rmse/max(base_h_rmse,0.001)):.1f}%)')

    print(f'\n  [中间量]')
    print(f'    水汽压修正均值: {de_pred.mean():.3f} hPa')
    print(f'    e_raw 均值:      {e_raw.mean():.3f} hPa')
    print(f'    e_cor 均值:      {e_corrected.mean():.3f} hPa')
    print(f'    露点均值:        {np.nanmean(T_dew):.2f}°C')
    print(f'    绝对湿度均值:    {np.mean(AH):.2f} g/m³')

    print(f'\n{"="*60}')
    print('考试总结')
    print(f'{"="*60}')
    print(f'  温度: RMSE={t_rmse:.4f}°C, MAE={t_mae:.4f}°C, R²={t_r2:.4f}')
    print(f'  湿度: RMSE={h_rmse:.4f}%,  MAE={h_mae:.4f}%,  R²={h_r2:.4f}')
    print()


if __name__ == '__main__':
    main()
