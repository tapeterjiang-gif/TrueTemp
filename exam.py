import warnings; warnings.filterwarnings('ignore')
import pandas as pd, numpy as np, joblib, sys
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score


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
        print(f'Filtered {before - len(df)} outliers:')
        for r in reasons:
            print(f'  - {r}')
        print()
    return df.copy()


def load_test_data(filepath):
    ref = pd.read_excel(filepath, sheet_name='REF')
    dut = pd.read_excel(filepath, sheet_name='DUT')
    ref['ts'] = pd.to_datetime(ref['timestamp'])
    dut['ts'] = pd.to_datetime(dut['received_at'])
    m = pd.merge(ref, dut, on='ts', how='inner').dropna(subset=['raw_temperature'])
    return filter_outliers(m)


def exam(model_temp, model_de, df, features):
    def es(T):
        return 6.112 * np.exp(17.67 * T / (T + 243.5))

    X = df[features]
    T_raw = df['raw_temperature'].values
    H_raw = df['raw_humidity'].values
    T_true = df['temperature'].values
    H_true = df['humidity'].values

    Tc = model_temp.predict(X)
    e_raw = (H_raw / 100) * es(T_raw)
    de = model_de.predict(X)
    Hc = np.clip(100 * (e_raw + de) / es(Tc), 0, 100)
    Hp = np.clip(100 * e_raw / es(Tc), 0, 100)

    dt = T_true - Tc
    dh = H_true - Hc
    adt = np.abs(dt)
    adh = np.abs(dh)

    def mtr(name, y_true, y_pred):
        rmse = np.sqrt(mean_squared_error(y_true, y_pred))
        mae = mean_absolute_error(y_true, y_pred)
        r2 = r2_score(y_true, y_pred)
        print(f'  {name:30s}  RMSE={rmse:.4f}  MAE={mae:.4f}  R2={r2:.4f}')

    print(f'Test samples: {len(df)}')
    print(f'T_true: {T_true.mean():.2f} ({T_true.min():.1f}~{T_true.max():.1f}) C')
    print(f'T_raw:  {T_raw.mean():.2f} ({T_raw.min():.1f}~{T_raw.max():.1f}) C')
    print(f'H_true: {H_true.mean():.2f} ({H_true.min():.1f}~{H_true.max():.1f}) %')
    print(f'H_raw:  {H_raw.mean():.2f} ({H_raw.min():.1f}~{H_raw.max():.1f}) %')

    print('\n--- Temperature ---')
    mtr('Raw (no comp)', T_true, T_raw)
    mtr('Two-Stage Coupled', T_true, Tc)
    print(f'  Max |err|: {adt.max():.4f} C  P95: {np.percentile(adt, 95):.4f} C')

    print('\n--- Humidity ---')
    mtr('Raw (no comp)', H_true, H_raw)
    mtr('Two-Stage Coupled', H_true, Hc)
    mtr('Pure Coupled (no de)', H_true, Hp)
    print(f'  Max |err|: {adh.max():.4f} %  P95: {np.percentile(adh, 95):.4f} %')

    print('\n--- Predictions ---')
    hdr = (f'{"#":>4s} {"RawT":>6s} {"RawH":>6s} {"TrueT":>6s} {"TrueH":>6s} '
           f'{"CompT":>6s} {"CompH":>6s} {"dT":>6s} {"dH":>6s}')
    print(hdr)
    print('-' * len(hdr))
    n = len(df)
    for idx in [0, n // 6, n // 3, n // 2, 2 * n // 3, 5 * n // 6, -1]:
        i = idx if idx >= 0 else n + idx
        if 0 <= i < n:
            print(f'{i:4d} {T_raw[i]:6.2f} {H_raw[i]:6.2f} {T_true[i]:6.2f} '
                  f'{H_true[i]:6.2f} {Tc[i]:6.2f} {Hc[i]:6.2f} '
                  f'{dt[i]:+6.2f} {dh[i]:+6.2f}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 exam.py <test_file.xlsx>')
        print('Example: python3 exam.py data/50D6_Data_241859.xlsx')
        sys.exit(1)

    filepath = sys.argv[1]
    print(f'Exam file: {filepath}')
    print('=' * 65)

    df = load_test_data(filepath)

    mt = joblib.load('models/model_temperature_2stage.pkl')
    md = joblib.load('models/model_vapor_pressure.pkl')

    features = ['raw_temperature', 'raw_humidity', 'temperature_soc', 'temperature_co2',
                'time_uptime', 'screen_brightness', 'screen_on_duration_s', 'cpu_load_10s',
                'wifi_load_10s', 'power_charge_ma', 'c5_soc_temp']

    exam(mt, md, df, features)
