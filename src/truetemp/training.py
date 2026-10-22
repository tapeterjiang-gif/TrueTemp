"""Train two small CPU LightGBM regressors using train/validation only."""
from pathlib import Path
import platform
import tomllib
import uuid
import numpy as np
import pandas as pd
import lightgbm as lgb
from .artifacts import digest, overwritable_directory, write_json
from .calibration import Calibration
from .dataset import load_split
from .evaluation import scores
from .features import DEFAULT, UNITS, build_features, validate_records
from .physics import PHYSICS, vapor_pressure, relative_humidity

DEFAULTS = dict(learning_rate=.03, max_depth=3, min_child_samples=20,
                max_iterations=1000, early_stopping=50, seed=42, num_threads=2,
                num_leaves=[7, 15], reg_lambda=[5.0, 10.0])


def settings(path):
    spec = tomllib.loads(Path(path).read_text()) if path else {}
    if set(spec) - {'training', 'features'}:
        raise ValueError('训练配置只支持 [training] 和 [features]')
    supplied = spec.get('training', {})
    if set(supplied) - set(DEFAULTS):
        raise ValueError('未知 training 配置项')
    config = DEFAULTS | supplied
    for key in ('max_depth', 'min_child_samples', 'max_iterations', 'early_stopping', 'num_threads'):
        if not isinstance(config[key], int) or config[key] <= 0:
            raise ValueError(f'{key} 必须为正整数')
    if not isinstance(config['seed'], int) or not 0 < config['learning_rate'] <= 1:
        raise ValueError('seed 或 learning_rate 无效')
    if not isinstance(config['num_leaves'], list) or not config['num_leaves'] or any(not isinstance(v, int) or v < 2 for v in config['num_leaves']):
        raise ValueError('num_leaves 需要 >=2 的整数列表')
    if not isinstance(config['reg_lambda'], list) or not config['reg_lambda'] or any(not np.isfinite(v) or v < 0 for v in config['reg_lambda']):
        raise ValueError('reg_lambda 需要非负数列表')
    if len(config['num_leaves'])*len(config['reg_lambda']) > 16:
        raise ValueError('首版最多比较 16 组参数')
    feature_config = spec.get('features', {})
    if set(feature_config) - {'disable'}:
        raise ValueError('features 仅支持 disable')
    disabled = feature_config.get('disable', [])
    if not isinstance(disabled, list) or set(disabled) - set(UNITS):
        raise ValueError('未知禁用特征')
    names = [name for name in DEFAULT if name not in disabled]
    if 'temperature_co2' in disabled or 'raw_temperature' in disabled:
        names = [name for name in names if name != 'co2_temperature_delta']
    return config, names, disabled


def train(dataset, output, config=None):
    params, names, disabled = settings(config)
    train_data, manifest = load_split(dataset, 'train')
    # 不使用 validation：全部训练数据参与拟合，使用默认参数，无早停、无网格搜索。
    sources = [s for s in manifest['sources'] if s['split'] == 'train']
    tr = validate_records(train_data)
    weights = tr.sample_weight.to_numpy()
    if (weights <= 0).any():
        raise ValueError('配对权重必须为正数')
    e_tr = vapor_pressure(tr.raw_temperature, tr.raw_humidity)
    baseline = dict(temperature=float(np.average(tr.ref_temperature-tr.raw_temperature, weights=weights)),
                    humidity=float(np.average(tr.ref_humidity-tr.raw_humidity, weights=weights)))
    meta = dict(schema_version=1, protocol_version=3, physics=PHYSICS, model_id=str(uuid.uuid4()),
                dut_ids=sorted(set(train_data.device_id.str.casefold().unique())), units=UNITS, sources=sources,
                baseline=baseline, coverage=manifest.get('coverage', {}), alignment=manifest['alignment'],
                models={}, input_ranges={}, dataset_files={k:v for k,v in manifest['files'].items() if k in ('train',)})
    models, runs, warnings = {}, {}, []
    if len(tr) < 2*params['min_child_samples']:
        warnings.append('训练样本不足以支持分裂；可能退化为常数基线，不代表精度达标')
    temp_pred = None
    for kind in ('temperature', 'vapor_pressure'):
        candidates = names + (['e_raw_hpa'] if kind == 'vapor_pressure' and 'e_raw_hpa' not in disabled else [])
        full = build_features(tr, candidates)
        active = [name for name in candidates if full[name].nunique() > 1]
        removed = [name for name in candidates if name not in active]
        x = full[active]
        if kind == 'temperature':
            y = tr.ref_temperature-tr.raw_temperature
            w = weights.copy()
            def metric(pred):
                return float(np.mean(abs(pred-(tr.ref_temperature-tr.raw_temperature).to_numpy())))
        else:
            y = vapor_pressure(tr.ref_temperature, tr.ref_humidity)-e_tr
            from .physics import saturation_pressure
            w = weights*(100/saturation_pressure(tr.ref_temperature))**2
            def metric(pred):
                return float(np.mean(abs(relative_humidity(temp_pred, e_tr+pred)-tr.ref_humidity)))
        w = w / w.mean()
        constant = float(np.average(y, weights=w))
        best_score = metric(np.full(len(tr), constant))
        best, best_params = None, None
        runs[kind] = [dict(kind='constant', train_mae=best_score)]
        if active and len(tr) >= 2*params['min_child_samples']:
            effective = dict(objective='regression', metric='None', learning_rate=params['learning_rate'],
                             max_depth=params['max_depth'], min_data_in_leaf=params['min_child_samples'],
                             num_leaves=params['num_leaves'][0], lambda_l2=params['reg_lambda'][0],
                             seed=params['seed'], num_threads=params['num_threads'], deterministic=True,
                             force_col_wise=True, verbosity=-1, feature_pre_filter=False)
            booster = lgb.train(effective, lgb.Dataset(x, label=y, weight=w),
                                num_boost_round=params['max_iterations'])
            best_score = metric(booster.predict(x))
            best, best_params = booster, effective
            runs[kind].append(dict(parameters=effective, best_iteration=booster.best_iteration or int(booster.num_trees())))
        meta['models'][kind] = dict(features=active, removed_constants=removed,
                                   constant=constant if best is None else None,
                                   best_parameters=best_params)
        if best is None:
            warnings.append(f'{kind}：训练集选择了常数残差基线')
        else:
            models[kind] = best
        if kind == 'temperature':
            temp_pred = tr.raw_temperature.to_numpy() + (best.predict(x) if best else constant)
    # Track required physical inputs even when training removes their constant columns.
    tracked = set(names) & set(tr.columns)
    tracked |= {'raw_temperature', 'raw_humidity'}
    meta['input_ranges'] = {key: [float(tr[key].min()), float(tr[key].max())] for key in sorted(tracked)}
    bundle = Calibration(meta, models)
    fitted = bundle.predict(tr)
    with overwritable_directory(output) as target:
        for kind, booster in models.items():
            booster.save_model(str(target / f'{kind}.txt'))
            meta['models'][kind]['sha256'] = digest(target / f'{kind}.txt')
        write_json(target / 'metadata.json', meta)
        write_json(target / 'training.json', dict(parameters=params, disabled_features=disabled,
                   warnings=warnings, candidates=runs, train_count=len(tr),
                   versions=dict(python=platform.python_version(), lightgbm=lgb.__version__, numpy=np.__version__, pandas=pd.__version__)))
        write_json(target / 'training_fit.json', dict(scores=scores(fitted, baseline),
                   quality_flags=fitted.quality_flags.value_counts().to_dict()))
    return meta['model_id'], warnings
