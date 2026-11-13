"""
温湿耦合补偿 — 两阶段高精度训练
=================================================

Stage 1: LightGBM → Temperature Compensation (T_comp)
Stage 2: LightGBM → Vapor Pressure Residual (δe = e_true - e_raw)

物理一致性:
  所有输出 (RH, Dew Point, AH) 统一从 e_corrected 和 T_comp 推导
  e_corrected = e_raw + δe(predicted)
"""

import warnings
warnings.filterwarnings('ignore')

import argparse
from bisect import bisect_left, bisect_right
import pandas as pd
import numpy as np
import json
from datetime import datetime
from pathlib import Path
from linux.infer_truetemp import FEATURES, SNOW2S_FEATURES

PAIR_WINDOW_MS = 2_000
DUPLICATE_CALLBACK_MS = 10


def select_dut_rows(dut, snow2s):
    if 'version' in dut:
        return dut.loc[pd.to_numeric(dut['version'], errors='raise') == (4 if snow2s else 3)].copy()
    return dut.iloc[:0].copy() if snow2s else dut


def distinct_callbacks(rows):
    rows = rows.copy()
    rows['monotonic_ms'] = pd.to_numeric(rows['monotonic_ms'], errors='raise')
    if not np.isfinite(rows['monotonic_ms']).all() or rows['payload_hex'].isna().any():
        raise ValueError('原始广播记录缺少有效的单调时间或载荷')
    rows = rows.sort_values('monotonic_ms', kind='stable')
    same_payload_gap = rows.groupby('payload_hex', sort=False)['monotonic_ms'].diff()
    return rows.loc[~same_payload_gap.le(DUPLICATE_CALLBACK_MS)]


def pair_by_reception(ref, dut, pair_window_ms=PAIR_WINDOW_MS):
    """在时间窗口内优先匹配时间差最小的报文，每条只用一次。"""
    ref_times = ref['monotonic_ms'].tolist()
    dut_times = dut['monotonic_ms'].tolist()
    candidates = []
    for i, received_ms in enumerate(ref_times):
        first = bisect_left(dut_times, received_ms - pair_window_ms)
        end = bisect_right(dut_times, received_ms + pair_window_ms)
        for j in range(first, end):
            candidates.append((abs(received_ms - dut_times[j]), i, j))

    used_ref, used_dut = set(), set()
    ref_indices, dut_indices, deltas = [], [], []
    for delta, i, j in sorted(candidates):
        if i not in used_ref and j not in used_dut:
            used_ref.add(i)
            used_dut.add(j)
            ref_indices.append(i)
            dut_indices.append(j)
            deltas.append(delta / 1000)

    columns_to_drop = ['monotonic_ms', 'payload_hex']
    merged = pd.concat([
        ref.iloc[ref_indices].drop(columns=columns_to_drop).reset_index(drop=True),
        dut.iloc[dut_indices].drop(columns=columns_to_drop).reset_index(drop=True),
    ], axis=1)
    merged['ts'] = pd.to_datetime(merged['timestamp'], utc=True).dt.tz_convert(
        'Asia/Shanghai').dt.tz_localize(None)
    return merged.assign(pair_delta_s=deltas, pair_method='reception')


def load_and_merge(filepaths, snow2s=False, *, report=False, pair_window_ms=PAIR_WINDOW_MS):
    dfs = []
    for fp in filepaths:
        with pd.ExcelFile(fp) as workbook:
            raw_sheets = {'REF_RAW', 'DUT_RAW'}
            available_raw = raw_sheets.intersection(workbook.sheet_names)
            if snow2s and available_raw:
                if available_raw != raw_sheets:
                    raise ValueError(f'{fp} 缺少 REF_RAW 或 DUT_RAW 工作表')
                ref = distinct_callbacks(workbook.parse('REF_RAW', dtype={'payload_hex': str}))
                dut = select_dut_rows(workbook.parse('DUT_RAW', dtype={'payload_hex': str}), snow2s)
                dut = distinct_callbacks(dut)
                m = pair_by_reception(ref, dut, pair_window_ms)
            else:
                ref = workbook.parse('REF')
                dut = select_dut_rows(workbook.parse('DUT'), snow2s)
                if dut.empty:
                    continue
                ref['ts'] = pd.to_datetime(ref['timestamp'])
                dut['ts'] = pd.to_datetime(dut['received_at'])
                m = pd.merge(ref, dut, on='ts', how='inner', validate='one_to_one' if snow2s else None)
                if snow2s:
                    m = m.assign(pair_delta_s=np.nan, pair_method='legacy')
        if snow2s:
            m = m.assign(source_file=str(fp))
            if report:
                mode = f'接收时间 ±{pair_window_ms / 1000:g} 秒' if available_raw else '旧表同秒'
                print(f'  {fp} · {mode} · REF {len(ref)} / DUT v4 {len(dut)} / 配对 {len(m)}')
        dfs.append(m)
    if not dfs or not any(not frame.empty for frame in dfs):
        raise ValueError('没有可用的 REF/DUT v4 配对样本' if snow2s else '没有可用的 REF/DUT v3 配对样本')
    merged = pd.concat(dfs, ignore_index=True)
    merged = merged.sort_values('ts').reset_index(drop=True)
    return merged if snow2s else filter_outliers(merged)


def filter_outliers(df):
    before = len(df)
    reasons = []

    mask = df['raw_humidity'] > 5.0
    if mask.sum() < before:
        reasons.append(f'raw_humidity <= 5%: {before - mask.sum()} rows')
    df = df[mask]

    mask = df['raw_humidity'] < 100.0
    if mask.sum() < len(df):
        reasons.append(f'raw_humidity >= 100%: {len(df) - mask.sum()} rows')
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


def prepare_features(df, snow2s=False):
    feature_cols = SNOW2S_FEATURES if snow2s else FEATURES
    X = df[feature_cols].copy()
    y_temp = df['temperature'].copy()
    y_humid = df['humidity'].copy()
    if snow2s:
        X = X.apply(pd.to_numeric, errors='raise')
        y_temp = pd.to_numeric(y_temp, errors='raise')
        y_humid = pd.to_numeric(y_humid, errors='raise')
        if not np.isfinite(X.to_numpy()).all() or not np.isfinite(y_temp).all():
            raise ValueError('Snow2S v4 的必需特征和参考温度必须为有限数值')
        for values, limit in ((y_humid, 100), (X.raw_humidity, 100), (X.screen_brightness, 1000),
                              (X.time_uptime, 7200), (X.screen_on_duration_s, 7200), (X.cpu_load_10s, 100),
                              (X.wifi_tx_bytes, 65535), (X.power_charge_ma, 65535)):
            if not values.between(0, limit).all():
                raise ValueError(f'{values.name} 超出 Snow2S v4 范围 0..{limit}')
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
    import lightgbm as lgb
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
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
    from sklearn.metrics import r2_score
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


def model_for_deploy(model, snow2s=False):
    """将 LightGBM 模型转成纯 Python / C 共用的嵌套树格式。"""
    dump = model.booster_.dump_model()
    def compact_tree(node):
        if 'leaf_value' in node:
            return {'v': node['leaf_value']}
        return {'f': node['split_feature'], 't': float(node['threshold']),
                'l': compact_tree(node['left_child']),
                'r': compact_tree(node['right_child'])}

    result = {
        'features': dump['feature_names'],
        'trees': [compact_tree(ti['tree_structure']) for ti in dump['tree_info']],
    }
    if snow2s:
        result['protocol_version'] = 4
    return result


def export_model_for_deploy(model_temp, model_de, output, snow2s):
    """导出为嵌套树 JSON 格式（供 exam_model.py / gen_c_model.py 使用）"""
    temp_json = model_for_deploy(model_temp, snow2s)
    de_json = model_for_deploy(model_de, snow2s)

    with (output / 'model_temperature.json').open('w') as f:
        json.dump(temp_json, f, separators=(',', ':'), ensure_ascii=False)
    with (output / 'model_vapor_pressure.json').open('w') as f:
        json.dump(de_json, f, separators=(',', ':'), ensure_ascii=False)

    print(f'\n  Exported: model_temperature.json '
          f'({len(json.dumps(temp_json, separators=(",",":")))} bytes)')
    print(f'  Exported: model_vapor_pressure.json '
          f'({len(json.dumps(de_json, separators=(",",":")))} bytes)')


def main():
    parser = argparse.ArgumentParser(description='训练温湿度两阶段模型')
    parser.add_argument('--snow2s', action='store_true', help='训练 Rockchip Linux 的 Snow2S DUT v4 模型')
    parser.add_argument('--pair-window-seconds', type=float,
                        help='Snow2S 原始广播配对容差（秒，默认 2）')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('files', type=Path, nargs='*', help='TrueTemp 采集的 REF/DUT XLSX 文件')
    args = parser.parse_args()
    if args.snow2s and not args.files:
        parser.error('--snow2s 需要显式指定包含 DUT v4 数据的 XLSX 文件')
    pair_window_ms = PAIR_WINDOW_MS
    if args.pair_window_seconds is not None:
        if not args.snow2s:
            parser.error('--pair-window-seconds 需要与 --snow2s 一起使用')
        pair_window_ms = args.pair_window_seconds * 1000
        if not np.isfinite(pair_window_ms) or pair_window_ms <= 0:
            parser.error('--pair-window-seconds 必须是有限正数')
    output = args.output_dir or Path('models/snow2s' if args.snow2s else 'models')
    if args.snow2s and output.resolve() == Path(__file__).resolve().parent / 'models':
        parser.error('Snow2S 模型必须写入独立目录')
    filepaths = args.files or sorted(Path('data').glob('*.xlsx'))
    print(f'Data files ({len(filepaths)}):')
    for fp in filepaths:
        print(f'  {fp}')

    print('Loading and merging data...')
    try:
        df = load_and_merge(filepaths, args.snow2s, report=True, pair_window_ms=pair_window_ms)
        X, y_temp, y_humid, features = prepare_features(df, args.snow2s)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    print(f'  Merged samples: {len(df)}')

    import joblib

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
    output.mkdir(parents=True, exist_ok=True)
    if args.snow2s:
        df[['source_file', 'pair_method', 'timestamp', 'received_at', 'pair_delta_s']].to_csv(
            output / 'pairs.csv', index=False)
    joblib.dump(model_temp, output / 'model_temperature_2stage.pkl')
    joblib.dump(model_de, output / 'model_vapor_pressure.pkl')

    export_model_for_deploy(model_temp, model_de, output, args.snow2s)

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
    if args.snow2s:
        metadata.update(
            protocol_version=4,
            raw_pair_window_seconds=pair_window_ms / 1000,
            input_ranges={key: [float(X[key].min()), float(X[key].max())] for key in features},
        )
    with (output / 'model_2stage_metadata.json').open('w') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    print(f'  Models: {output.resolve()}')

    print('\nDone.')


if __name__ == '__main__':
    main()
