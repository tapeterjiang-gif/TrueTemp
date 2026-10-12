"""
QingThermal 温湿耦合补偿 — 两阶段高精度训练
=================================================

Stage 1: LightGBM → Temperature Compensation (T_comp)
Stage 2: LightGBM → Vapor Pressure Residual (δe = e_true - e_raw)

物理一致性:
  所有输出 (RH, Dew Point, AH) 统一从 e_corrected 和 T_comp 推导
  e_corrected = e_raw + δe(predicted)
"""

import warnings
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np
import lightgbm as lgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
import joblib
import json
from datetime import datetime


def load_and_merge(filepaths):
    dfs = []
    for fp in filepaths:
        ref = pd.read_excel(fp, sheet_name='REF')
        dut = pd.read_excel(fp, sheet_name='DUT')
        ref['ts'] = pd.to_datetime(ref['timestamp'])
        dut['ts'] = pd.to_datetime(dut['received_at'])
        m = pd.merge(ref, dut, on='ts', how='inner')
        dfs.append(m)
    merged = pd.concat(dfs, ignore_index=True)
    merged = merged.sort_values('ts').reset_index(drop=True)
    merged = filter_outliers(merged)
    return merged


def filter_outliers(df):
    before = len(df)
    reasons = []

    mask = df['raw_humidity'] > 5.0
    if mask.sum() < before:
        reasons.append(f'raw_humidity <= 5%: {before - mask.sum()} rows')
    df = df[mask]

    mask = df['raw_humidity'] < 50.0
    if mask.sum() < len(df):
        reasons.append(f'raw_humidity >= 50%: {len(df) - mask.sum()} rows')
    df = df[mask]

    mask = df['humidity'] > 10.0
    if mask.sum() < len(df):
        reasons.append(f'ref humidity <= 10%: {len(df) - mask.sum()} rows')
    df = df[mask]

    mask = df['humidity'] < 100.0
    if mask.sum() < len(df):
        reasons.append(f'ref humidity >= 100%: {len(df) - mask.sum()} rows')
    df = df[mask]

    mask = df['raw_temperature'] > 20.0
    if mask.sum() < len(df):
        reasons.append(f'raw_temperature <= 20C: {len(df) - mask.sum()} rows')
    df = df[mask]

    mask = df['cpu_load_10s'] >= 0
    if mask.sum() < len(df):
        reasons.append(f'cpu_load_10s < 0: {len(df) - mask.sum()} rows')
    df = df[mask]

    if reasons:
        print(f'  Filtered {before - len(df)} outliers:')
        for r in reasons:
            print(f'    - {r}')
    return df.copy()


def prepare_features(df):
    # 新数据结构 (20260912_2328.xlsx): DUT 全部可用列, temperature_soc/co2 单位 °C
    feature_cols = [
        'raw_temperature', 'raw_humidity', 'temperature_soc', 'temperature_co2',
        'time_uptime', 'screen_brightness', 'screen_on_duration_s', 'cpu_load_10s',
        'wifi_load_10s', 'power_charge_ma', 'c5_soc_temp',
    ]
    X = df[feature_cols].copy()
    y_temp = df['temperature'].copy()
    y_humid = df['humidity'].copy()
    return X, y_temp, y_humid, feature_cols


def lgb_params():
    return {
        'objective': 'regression',
        'metric': 'rmse',
        'boosting_type': 'gbdt',
        'num_leaves': 15,
        'max_depth': 5,
        'learning_rate': 0.05,
        'n_estimators': 500,
        'min_data_in_leaf': 8,
        'feature_fraction': 0.8,
        'bagging_fraction': 0.8,
        'bagging_freq': 5,
        'lambda_l1': 0.1,
        'lambda_l2': 0.1,
        'verbose': -1,
        'random_state': 42,
        'n_jobs': -1,
    }


def train_with_cv(X, y, target_name):
    tscv = TimeSeriesSplit(n_splits=3)
    params = lgb_params()
    cv_scores = []

    print(f'\n{"="*60}')
    print(f'Training: {target_name}')
    print(f'{"="*60}')

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X), 1):
        X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
        y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]

        model = lgb.LGBMRegressor(**params)
        model.fit(X_train, y_train,
                  eval_set=[(X_val, y_val)], eval_metric='rmse',
                  callbacks=[lgb.early_stopping(stopping_rounds=50),
                             lgb.log_evaluation(period=0)])

        y_pred = model.predict(X_val)
        rmse = np.sqrt(mean_squared_error(y_val, y_pred))
        mae = mean_absolute_error(y_val, y_pred)
        r2 = r2_score(y_val, y_pred)
        cv_scores.append({'rmse': rmse, 'mae': mae, 'r2': r2})
        print(f'  Fold {fold} (train={len(X_train)}, val={len(X_val)}): '
              f'RMSE={rmse:.4f}, MAE={mae:.4f}, R\u00b2={r2:.4f}')

    avg_rmse = np.mean([s['rmse'] for s in cv_scores])
    avg_mae = np.mean([s['mae'] for s in cv_scores])
    print(f'  {"—"*40}')
    print(f'  Average: RMSE={avg_rmse:.4f}, MAE={avg_mae:.4f}')

    final_model = lgb.LGBMRegressor(**params)
    final_model.fit(X, y)
    return final_model, cv_scores


def saturation_vapor_pressure(T):
    return 6.112 * np.exp(17.67 * T / (T + 243.5))


def evaluate_two_stage(model_temp, model_de, X, y_true_temp, y_true_humid, feature_cols):
    T_comp = model_temp.predict(X)

    raw_temp_idx = feature_cols.index('raw_temperature')
    raw_humid_idx = feature_cols.index('raw_humidity')
    T_raw = X.iloc[:, raw_temp_idx].values
    RH_raw = X.iloc[:, raw_humid_idx].values

    e_raw = (RH_raw / 100.0) * saturation_vapor_pressure(T_raw)

    de_pred = model_de.predict(X)
    e_corrected = e_raw + de_pred

    es_comp = saturation_vapor_pressure(T_comp)
    RH_comp = np.clip(100.0 * e_corrected / es_comp, 0.0, 100.0)

    AH = 216.7 * e_corrected / (T_comp + 273.15)

    with np.errstate(divide='ignore', invalid='ignore'):
        gamma = np.log(e_corrected / 6.112)
    T_dew = np.where(e_corrected > 0.06, 243.5 * gamma / (17.67 - gamma), np.nan)

    temp_resid = y_true_temp.values - T_comp
    humid_resid = y_true_humid.values - RH_comp

    temp_rmse = np.sqrt(np.mean(temp_resid ** 2))
    temp_mae = np.mean(np.abs(temp_resid))
    temp_r2 = r2_score(y_true_temp, T_comp)

    humid_rmse = np.sqrt(np.mean(humid_resid ** 2))
    humid_mae = np.mean(np.abs(humid_resid))
    humid_r2 = r2_score(y_true_humid, RH_comp)

    print(f'\n{"="*60}')
    print(f'Two-Stage Model Evaluation (full dataset)')
    print(f'{"="*60}')
    print(f'  {"Temperature":20s}  RMSE={temp_rmse:.4f}°C  MAE={temp_mae:.4f}°C  R\u00b2={temp_r2:.4f}')
    print(f'  {"Humidity":20s}  RMSE={humid_rmse:.4f}%   MAE={humid_mae:.4f}%   R\u00b2={humid_r2:.4f}')
    print(f'  {"Vapor Pressure Corr":20s}  mean δe={de_pred.mean():.3f} hPa, std={de_pred.std():.3f} hPa')
    print(f'  {"Dew Point":20s}  mean={np.nanmean(T_dew):.2f}°C')
    print(f'  {"Absolute Humidity":20s}  mean={np.mean(AH):.2f} g/m\u00b3')

    return {
        'temperature': {'rmse': temp_rmse, 'mae': temp_mae, 'r2': temp_r2},
        'humidity': {'rmse': humid_rmse, 'mae': humid_mae, 'r2': humid_r2},
    }


def demo_predictions(model_temp, model_de, X, feature_cols, y_true_temp=None, y_true_humid=None):
    T_raw = X.iloc[:, feature_cols.index('raw_temperature')].values
    RH_raw = X.iloc[:, feature_cols.index('raw_humidity')].values

    T_comp = model_temp.predict(X)
    e_raw = (RH_raw / 100.0) * saturation_vapor_pressure(T_raw)
    de_pred = model_de.predict(X)
    e_cor = e_raw + de_pred
    es_comp = saturation_vapor_pressure(T_comp)
    RH_comp = np.clip(100.0 * e_cor / es_comp, 0.0, 100.0)
    AH = 216.7 * e_cor / (T_comp + 273.15)

    with np.errstate(divide='ignore', invalid='ignore'):
        gamma = np.log(e_cor / 6.112)
    T_dew = np.where(e_cor > 0.06, 243.5 * gamma / (17.67 - gamma), np.nan)

    print(f'\n{"="*85}')
    print('Compensation demo (first 5 samples)')
    print(f'{"="*85}')
    hdr = (f'{"RawT":>6s} {"RawH":>5s} {"TrueT":>6s} {"TrueH":>6s} '
           f'{"CompT":>6s} {"CompH":>6s} {"Dew":>6s} {"AH":>7s} {"e_cor":>6s}')
    print(hdr)
    print('-' * len(hdr))
    for i in range(min(5, len(T_comp))):
        tt = y_true_temp.values[i] if y_true_temp is not None else float('nan')
        th = y_true_humid.values[i] if y_true_humid is not None else float('nan')
        print(f'{T_raw[i]:6.2f} {RH_raw[i]:5.2f} {tt:6.2f} {th:6.2f} '
              f'{T_comp[i]:6.2f} {RH_comp[i]:6.2f} {T_dew[i]:6.2f} '
              f'{AH[i]:7.3f} {e_cor[i]:6.2f}')


def export_model_for_deploy(model_temp, model_de, feature_cols):
    """导出为嵌套树 JSON 格式（供 infer_coupled 使用）"""
    d_temp = model_temp.booster_.dump_model()
    d_de = model_de.booster_.dump_model()

    def compact_tree(node):
        if 'leaf_value' in node:
            return {'v': node['leaf_value']}
        return {'f': node['split_feature'], 't': float(node['threshold']),
                'l': compact_tree(node['left_child']),
                'r': compact_tree(node['right_child'])}

    temp_json = {
        'features': d_temp['feature_names'],
        'trees': [compact_tree(ti['tree_structure']) for ti in d_temp['tree_info']],
    }
    de_json = {
        'features': d_de['feature_names'],
        'trees': [compact_tree(ti['tree_structure']) for ti in d_de['tree_info']],
    }

    with open('models/model_temperature.json', 'w') as f:
        json.dump(temp_json, f, separators=(',', ':'), ensure_ascii=False)
    with open('models/model_vapor_pressure.json', 'w') as f:
        json.dump(de_json, f, separators=(',', ':'), ensure_ascii=False)

    print(f'\n  Exported: model_temperature.json '
          f'({len(json.dumps(temp_json, separators=(",",":")))} bytes)')
    print(f'  Exported: model_vapor_pressure.json '
          f'({len(json.dumps(de_json, separators=(",",":")))} bytes)')


def main():
    filepaths = [
        'data/20260912_2328.xlsx',
    ]

    print('Loading and merging data...')
    df = load_and_merge(filepaths)
    print(f'  Merged samples: {len(df)}')

    X, y_temp, y_humid, features = prepare_features(df)

    T_raw = X['raw_temperature'].values
    RH_raw = X['raw_humidity'].values
    Y_h = y_humid.values
    Y_t = y_temp.values

    e_raw = (RH_raw / 100.0) * saturation_vapor_pressure(T_raw)
    e_true = (Y_h / 100.0) * saturation_vapor_pressure(Y_t)
    de = pd.Series(e_true - e_raw, index=X.index)

    model_temp, cv_temp = train_with_cv(X, y_temp, 'Temperature (Stage 1)')
    model_de, cv_de = train_with_cv(X, de, 'Vapor Pressure Residual δe (Stage 2)')

    eval_result = evaluate_two_stage(model_temp, model_de, X, y_temp, y_humid, features)
    demo_predictions(model_temp, model_de, X, features, y_temp, y_humid)

    print(f'\n{"="*60}')
    print('Saving models...')
    print(f'{"="*60}')
    joblib.dump(model_temp, 'models/model_temperature_2stage.pkl')
    joblib.dump(model_de, 'models/model_vapor_pressure.pkl')
    print('  Saved: models/model_temperature_2stage.pkl')
    print('  Saved: models/model_vapor_pressure.pkl')

    export_model_for_deploy(model_temp, model_de, features)

    metadata = {
        'created_at': datetime.now().isoformat(),
        'model_type': 'two_stage_thermo_humidity',
        'samples': len(df),
        'features': features,
        'stage1_temperature': {'cv_results': cv_temp, 'full_eval': eval_result['temperature']},
        'stage2_vapor_pressure_residual': {
            'cv_results': cv_de,
            'delta_e_mean': float(de.mean()),
            'delta_e_std': float(de.std()),
        },
        'combined': eval_result,
    }
    with open('models/model_2stage_metadata.json', 'w') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    print('  Saved: model_2stage_metadata.json')

    print('\nDone.')


if __name__ == '__main__':
    main()
