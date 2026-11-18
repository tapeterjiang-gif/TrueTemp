"""从已发布的 Snow2S 配对表训练 9 项温差分支，保留原绝对温度与水汽压模型。"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from gen_c_model import gen_linux_data
from linux.infer_truetemp import (
    DELTA_GATE_STRATEGY, SNOW2S_FEATURES, SNOW2S_DELTA_FEATURES, SNOW2S_HEAT_FEATURES,
    load_trees, load_temperature_policy,
)
from train_coupled_model import lgb_params, model_for_deploy, prepare_features, select_dut_rows


ROOT = Path(__file__).resolve().parent
PAIR_COLUMNS = ['source_file', 'pair_method', 'timestamp', 'received_at', 'pair_delta_s']


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replay_pairs(pairs_path, source_root=ROOT):
    """按保存的两侧时间戳回查标签及特征，后续追加的采集行不参与训练。"""
    pairs = pd.read_csv(pairs_path)
    if pairs.empty or not set(PAIR_COLUMNS).issubset(pairs.columns):
        raise ValueError('配对表为空或缺少来源、时间戳及配对方式')
    if pairs[PAIR_COLUMNS[:4]].isna().any().any():
        raise ValueError('配对表存在空来源或时间戳')
    for timestamp in ('timestamp', 'received_at'):
        if pairs.duplicated(['source_file', timestamp]).any():
            raise ValueError(f'配对表重复使用 {timestamp}')
    frames = []
    sources = []
    for filename, group in pairs.groupby('source_file', sort=False):
        path = source_root / filename
        # 旧配对表记录的 RK3506 工作簿原先直接位于 data/。
        if not path.exists() and Path(filename).parent == Path('data'):
            path = source_root / 'data' / 'rk3506' / path.name
        methods = group.pair_method.unique().tolist()
        if len(methods) != 1 or methods[0] not in ('reception', 'legacy'):
            raise ValueError(f'{filename}: 配对方式无效或混用')
        suffix = '_RAW' if methods[0] == 'reception' else ''
        with pd.ExcelFile(path) as workbook:
            ref = workbook.parse(f'REF{suffix}')[['timestamp', 'temperature', 'humidity']]
            dut = select_dut_rows(workbook.parse(f'DUT{suffix}'), snow2s=True)
            dut = dut[['received_at', *SNOW2S_FEATURES]]
        # Same-timestamp callbacks may repeat, but their selected values must agree.
        ref, dut = ref.drop_duplicates(), dut.drop_duplicates()
        if ref.timestamp.duplicated().any() or dut.received_at.duplicated().any():
            raise ValueError(f'{filename}: 同一时间戳对应多个不同读数')
        merged = group[PAIR_COLUMNS].merge(ref, on='timestamp', how='left', validate='one_to_one')
        merged = merged.merge(dut, on='received_at', how='left', validate='one_to_one')
        if merged[['temperature', 'humidity', *SNOW2S_FEATURES]].isna().any().any():
            raise ValueError(f'{filename}: 无法完整恢复配对读数')
        frames.append(merged)
        sources.append({'source_file': filename, 'sha256': sha256(path), 'pairs': len(merged)})
    result = pd.concat(frames, ignore_index=True)
    result['ts'] = pd.to_datetime(result.timestamp, utc=True)
    return result.sort_values('ts', kind='stable').reset_index(drop=True), sources


def build_candidate(base_dir, output_dir, source_root=ROOT, *, heat_gate_max=None, shrink_fraction=0.75):
    if heat_gate_max is not None and not np.isfinite(heat_gate_max):
        raise ValueError('heat_gate_max 必须为有限数值')
    if not np.isfinite(shrink_fraction) or not 0 <= shrink_fraction <= 1:
        raise ValueError('shrink_fraction 必须在 0..1 之间')

    import lightgbm as lgb

    base_dir, output_dir = Path(base_dir).resolve(), Path(output_dir).resolve()
    if output_dir.exists():
        raise ValueError(f'输出目录已存在，请指定一个新候选目录: {output_dir}')
    temp_path = base_dir / 'model_temperature.json'
    de_path = base_dir / 'model_vapor_pressure.json'
    load_trees(temp_path, 500, snow2s=True)
    load_trees(de_path, 500, snow2s=True)
    if load_temperature_policy(temp_path, snow2s=True) is not None:
        raise ValueError('基础模型已经包含温差分支，请使用原绝对温度模型')
    metadata_path = base_dir / 'model_2stage_metadata.json'
    base_metadata = json.loads(metadata_path.read_text())
    frame, sources = replay_pairs(base_dir / 'pairs.csv', source_root)
    X, y, _, _ = prepare_features(frame, snow2s=True)
    ranges = {key: [float(X[key].min()), float(X[key].max())] for key in SNOW2S_FEATURES}
    if len(frame) != base_metadata['samples'] or ranges != base_metadata['input_ranges']:
        raise ValueError('恢复的样本数或特征范围与基础模型元数据不一致')
    delta = X.raw_temperature - y
    delta_median = float(delta.median())
    heat_gate_max = float(delta.quantile(0.75)) if heat_gate_max is None else float(heat_gate_max)
    if heat_gate_max < delta_median:
        raise ValueError('heat_gate_max 不能低于训练温差中位数')
    params = dict(lgb_params(), n_jobs=2)
    delta_model = lgb.LGBMRegressor(**params).fit(X[SNOW2S_DELTA_FEATURES], delta)
    heat_model = lgb.LGBMRegressor(**params).fit(X[SNOW2S_HEAT_FEATURES], delta)
    policy = {
        'strategy': DELTA_GATE_STRATEGY,
        'raw_temperature_max': float(X.raw_temperature.max()),
        'delta_median': delta_median,
        'heat_gate_max': heat_gate_max,
        'shrink_fraction': float(shrink_fraction),
        'delta_model': model_for_deploy(delta_model, snow2s=True),
        'heat_model': model_for_deploy(heat_model, snow2s=True),
    }
    temperature = json.loads(temp_path.read_text())
    temperature['temperature_policy'] = policy
    metadata = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'model_type': DELTA_GATE_STRATEGY,
        'protocol_version': 4,
        'samples': len(frame),
        'features': SNOW2S_FEATURES,
        'input_ranges': ranges,
        'reference_temperature_range': [float(y.min()), float(y.max())],
        'temperature_policy': {key: value for key, value in policy.items() if not key.endswith('_model')},
        'delta_features': SNOW2S_DELTA_FEATURES,
        'heat_features': SNOW2S_HEAT_FEATURES,
        'training_parameters': params,
        'delta_training_mae': float(np.mean(np.abs(delta_model.predict(X[SNOW2S_DELTA_FEATURES]) - delta))),
        'heat_training_mae': float(np.mean(np.abs(heat_model.predict(X[SNOW2S_HEAT_FEATURES]) - delta))),
        'source_model': str(base_dir),
        'source_hashes': {p.name: sha256(p) for p in [temp_path, de_path, metadata_path, base_dir / 'pairs.csv']},
        'source_files': sources,
        'source_pair_window_seconds': base_metadata.get('raw_pair_window_seconds'),
        'pairing': 'replay saved source_file / REF timestamp / DUT timestamp; no new pairing',
        'command': [sys.executable, *sys.argv],
        'versions': {'python': sys.version.split()[0], 'lightgbm': lgb.__version__,
                     'numpy': np.__version__, 'pandas': pd.__version__},
        'code_git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'code_sha256': {name: sha256(ROOT / name) for name in (
            'train_snow2s_hybrid.py', 'train_coupled_model.py', 'gen_c_model.py',
            'linux/infer_truetemp.py', 'linux/truetemp.c', 'linux/truetemp.h',
        )},
        'validation_scope': 'Training replay only; higher ambient temperatures require independent field validation.',
    }

    # Stage all artifacts before publishing a new directory, so a failed run leaves no partial candidate.
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.snow2s-hybrid-', dir=output_dir.parent) as staging:
        candidate = Path(staging) / 'candidate'
        candidate.mkdir()
        (candidate / temp_path.name).write_text(json.dumps(temperature, separators=(',', ':')))
        shutil.copyfile(de_path, candidate / de_path.name)
        shutil.copyfile(base_dir / 'pairs.csv', candidate / 'pairs.csv')
        frame.drop(columns='ts').to_csv(candidate / 'training_pairs.csv', index=False)
        (candidate / 'model_2stage_metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
        gen_linux_data(str(candidate / temp_path.name), str(candidate / de_path.name),
                       str(candidate / 'truetemp_snow2s_model_data.h'), snow2s=True)
        # These files form a drop-in C source package with the existing Snow2S ABI.
        for name in ('truetemp.c', 'truetemp.h', 'truetemp_infer.c', 'infer_truetemp.py'):
            shutil.copyfile(ROOT / 'linux' / name, candidate / name)
        candidate.rename(output_dir)
    print(f'Generated: {output_dir} ({len(frame)} saved pairs)')
    print(json.dumps(metadata['temperature_policy'], ensure_ascii=False))
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-dir', type=Path, default=ROOT / 'models/snow2s-20261112')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, default=ROOT,
                        help='配对表中的原始采集文件路径相对于此目录')
    parser.add_argument('--heat-gate-max', type=float,
                        help='允许少扣的四项预测温差上限，单位 ℃；默认使用训练温差第 75 百分位')
    parser.add_argument('--shrink-fraction', type=float, default=0.75,
                        help='超出训练温差中位数部分的少扣比例，0..1；默认 0.75')
    args = parser.parse_args()
    try:
        build_candidate(args.base_dir, args.output_dir, args.source_root,
                        heat_gate_max=args.heat_gate_max, shrink_fraction=args.shrink_fraction)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
