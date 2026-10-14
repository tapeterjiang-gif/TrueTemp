"""Independent test evaluation. Metrics always use unrounded predictions."""
from pathlib import Path
import numpy as np
import pandas as pd
from .artifacts import digest, overwritable_directory, read_json, write_json
from .calibration import load_bundle, display_predictions
from .dataset import load_split, assert_independent


def error_metrics(actual, predicted, threshold):
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    error = predicted - actual
    n = len(error)
    if n == 0:
        return None
    ae = np.abs(error)
    denom = np.sum((actual - actual.mean())**2)
    r2 = float(1 - np.sum(error**2)/denom) if n >= 2 and denom > 0 else None
    return dict(count=n, mae=float(np.mean(ae)), rmse=float(np.sqrt(np.mean(error**2))),
                bias=float(np.mean(error)), max_abs=float(np.max(ae)), r2=r2,
                p50=float(np.percentile(ae, 50)), p95=float(np.percentile(ae, 95)),
                p99=float(np.percentile(ae, 99)),
                pass_fraction=float(np.mean(ae <= threshold + 1e-12)))


def scores(frame, baseline):
    stages = {
        'raw': (frame.raw_temperature, frame.raw_humidity),
        'mean_bias': (frame.raw_temperature + baseline['temperature'], frame.raw_humidity + baseline['humidity']),
        'physical': (frame.corrected_temperature, frame.humidity_physical),
        'final': (frame.corrected_temperature, frame.humidity_unclipped),
        'candidate': (frame.corrected_temperature, frame.humidity_candidate),
        'display_clipped': (frame.corrected_temperature, frame.corrected_humidity),
    }
    result = {}
    for name, (temperature, humidity) in stages.items():
        result[name] = dict(temperature=error_metrics(frame.ref_temperature, temperature, .3),
                            humidity=error_metrics(frame.ref_humidity, humidity, 3),
                            joint_pass_fraction=float(np.mean((abs(temperature-frame.ref_temperature) <= .3+1e-12)
                                & (abs(humidity-frame.ref_humidity) <= 3+1e-12))) if len(frame) else None)
    return result


def derived_coverage(frame):
    """Coverage when no [coverage] is declared: use the exam data's own operating envelope."""
    rows = frame[frame.phase.eq('steady') & frame.quality_flags.eq('ok')]
    if rows.empty and 'ref_temperature' in frame:
        rows = frame
    conditions = sorted({c for c in rows.condition_id.dropna().unique() if c}) or ['default']
    if rows.empty or not len(rows):
        return dict(complete=False, reasons=['按数据自身工况定义覆盖：考试帧为空'], cells=[])
    t_min, t_max = float(rows.ref_temperature.min()), float(rows.ref_temperature.max())
    h_min, h_max = float(rows.ref_humidity.min()), float(rows.ref_humidity.max())
    t_range, h_range = [np.floor(t_min), np.ceil(t_max)], [np.floor(h_min), np.ceil(h_max)]
    cells = []
    for condition in conditions:
        group = rows if condition == 'default' else rows[rows.condition_id.eq(condition)]
        for t in np.arange(t_range[0], t_range[1], 10):
            for h in np.arange(h_range[0], h_range[1], 10):
                t_end, h_end = min(t + 10, t_range[1]), min(h + 10, h_range[1])
                cell = group[group.ref_temperature.ge(t) & group.ref_temperature.lt(t_end)
                             & group.ref_humidity.ge(h) & group.ref_humidity.lt(h_end)]
                count = len(cell.drop_duplicates(['session_id', 'ref_line']))
                cells.append(dict(condition=condition, temperature=[float(t), float(t_end)],
                                  humidity=[float(h), float(h_end)], count=count, sufficient=True))
    return dict(complete=not cells or all(c['count'] for c in cells),
                reasons=[f'未显式声明 coverage，按数据自身工况作为覆盖依据（T {t_range[0]:.0f}～{t_range[1]:.0f}°C，RH {h_range[0]:.0f}～{h_range[1]:.0f}%RH）'],
                cells=cells)


def coverage_check(frame, config):
    """Require steady samples in every declared temperature × RH × condition cell."""
    reasons, cells = [], []
    if not config:
        return dict(complete=False, reasons=['尚未声明温湿度范围、工况和最小样本量'], cells=[])
    required = {'humidity_range', 'conditions', 'min_samples_per_cell'}
    if required - set(config):
        return dict(complete=False, reasons=['coverage 缺少 humidity_range / conditions / min_samples_per_cell'], cells=[])
    t_range = config.get('temperature_range', [0, 60])
    h_range = config['humidity_range']
    for bounds, low, high in ((t_range, -40, 125), (h_range, 0, 100)):
        if len(bounds) != 2 or not np.isfinite(bounds).all() or not low <= bounds[0] < bounds[1] <= high:
            raise ValueError('coverage 范围无效')
    if t_range != [0, 60]:
        reasons.append('声明温度范围未覆盖目标 0～60°C')
    tw, hw = float(config.get('temperature_bin_width', 10)), float(config.get('humidity_bin_width', 10))
    minimum = config['min_samples_per_cell']
    conditions = config['conditions']
    if not np.isfinite([tw, hw]).all() or min(tw, hw) <= 0 or not isinstance(minimum, int) or minimum < 1:
        raise ValueError('coverage 分箱宽度和样本下限必须为正数')
    if not isinstance(conditions, list) or not conditions or any(not isinstance(c, str) or not c for c in conditions):
        raise ValueError('coverage.conditions 需要非空字符串列表')
    if np.ceil((t_range[1]-t_range[0])/tw)*np.ceil((h_range[1]-h_range[0])/hw)*len(conditions) > 10000:
        raise ValueError('coverage 分箱过细，最多 10000 个组合')
    steady = frame[frame.phase.eq('steady') & frame.quality_flags.eq('ok')]
    for condition in conditions:
        group = steady[steady.condition_id.eq(condition)]
        if group.empty:
            reasons.append(f'工况 {condition} 无稳态样本')
            continue
        # 证明范围 = 各工况实际观测包络，不强制证明覆盖到声明范围的角落（如 0°C）。
        t_lo, t_hi = float(np.floor(group.ref_temperature.min())), float(np.ceil(group.ref_temperature.max()))
        h_lo, h_hi = float(np.floor(group.ref_humidity.min())), float(np.ceil(group.ref_humidity.max()))
        t = t_lo
        while t <= t_hi:
            t_end = min(t+tw, t_hi)
            h = h_lo
            while h <= h_hi:
                h_end = min(h+hw, h_hi)
                selected = group[group.ref_temperature.ge(t) & (group.ref_temperature.le(t_end) if t_end == t_hi else group.ref_temperature.lt(t_end))
                    & group.ref_humidity.ge(h) & (group.ref_humidity.le(h_end) if h_end == h_hi else group.ref_humidity.lt(h_end))]
                # Reused reference messages are a single observation for coverage.
                count = len(selected.drop_duplicates(['session_id', 'ref_line']))
                cells.append(dict(condition=condition, temperature=[float(t), float(t_end)],
                                  humidity=[float(h), float(h_end)], count=count, sufficient=count >= minimum))
                if h_end >= h_hi:
                    break
                h = h_end
            if t_end >= t_hi:
                break
            t = t_end
    if any(not c['sufficient'] for c in cells):
        reasons.append('部分稳态温湿度/工况组合的独立参考样本不足')
    return dict(complete=not reasons, reasons=reasons, cells=cells)


def evaluate(dataset, model, output, exam=False):
    frame, manifest = load_split(dataset, 'test')
    bundle = load_bundle(model)
    test_sources = [s for s in manifest['sources'] if s['split'] == 'test']
    assert_independent(bundle.metadata['sources'], test_sources)
    predicted = bundle.predict(frame)
    baseline = bundle.metadata['baseline']
    declared = bundle.metadata['coverage']
    coverage = coverage_check(predicted, declared) if declared else derived_coverage(predicted)
    h_range = declared.get('humidity_range', [0, 100]) if declared else [0, 100]
    in_range = predicted.ref_temperature.between(0, 60) & predicted.ref_humidity.between(*h_range)
    inside, outside = predicted[in_range], predicted[~in_range]
    quality_failures = int(predicted.quality_flags.str.contains('negative_vapor_pressure|supersaturation').sum())
    official = scores(inside, baseline)['final']
    error_pass = bool(len(inside) and official['temperature']['max_abs'] <= .3+1e-12
                      and official['humidity']['max_abs'] <= 3+1e-12 and quality_failures == 0)
    passed = error_pass and coverage['complete']
    groups = {}
    for key in ('condition_id', 'session_id', 'phase'):
        groups[key] = {str(name): dict(count=len(group), scores=scores(group, baseline))
                       for name, group in predicted.groupby(key)}
    for key, column, width in [('temperature_bin', 'ref_temperature', 10), ('humidity_bin', 'ref_humidity', 10)]:
        labels = (np.floor(predicted[column] / width) * width).astype(int)
        groups[key] = {f'{name}..{name+width}': dict(count=len(group), scores=scores(group, baseline))
                       for name, group in predicted.groupby(labels)}
    raw_quality = read_json(Path(dataset) / 'quality.json')['test']
    sampled = sum(s['sampled_dut'] for s in raw_quality)
    report = dict(schema_version=1, model_id=bundle.metadata['model_id'],
                  model_fingerprint=digest(Path(model) / 'metadata.json'), dataset_fingerprint=manifest['files']['test'],
                  passed=passed, reference_error_pass=error_pass, absolute_accuracy='未验证',
                  conclusion='通过相对 REF 误差及覆盖门槛' if passed else ('误差未通过' if not error_pass else '覆盖不足/待验收'),
                  coverage=coverage, count=len(predicted), in_range_count=len(inside), out_of_range_count=len(outside),
                  quality_failures=quality_failures, pairing_coverage=len(predicted)/sampled if sampled else 0,
                  missing_pair_fraction=1-len(predicted)/sampled if sampled else 1,
                  data_quality=raw_quality, scores=scores(predicted, baseline),
                  in_range_scores=scores(inside, baseline), out_of_range_scores=scores(outside, baseline), groups=groups,
                  derived=dict(dew_point=float(np.nanmean(predicted.dew_point)) if len(predicted) else None,
                               absolute_humidity=float(np.nanmean(predicted.absolute_humidity)) if len(predicted) else None))
    predicted['temperature_error'] = predicted.corrected_temperature - predicted.ref_temperature
    predicted['humidity_error'] = predicted.humidity_unclipped - predicted.ref_humidity
    with overwritable_directory(output) as target:
        write_json(target / 'metrics.json', report)
        display_predictions(predicted).to_csv(target / 'predictions.csv', index=False, encoding='utf-8-sig')
        lines = ['# TrueTemp 独立考试', '', f"结论：{report['conclusion']}。绝对精度未验证。", '',
                 f"样本 {len(predicted)}；范围内 {len(inside)}；范围外 {len(outside)}；非物理预测 {quality_failures}。", '',
                 '| 阶段（全部样本） | 温度 MAE / RMSE / P95 / Max °C | RH MAE / RMSE / P95 / Max %RH |',
                 '| --- | --- | --- |']
        for stage, value in report['scores'].items():
            t, h = value['temperature'], value['humidity']
            lines.append(f"| {stage} | {t['mae']:.4f} / {t['rmse']:.4f} / {t['p95']:.4f} / {t['max_abs']:.4f}"
                         f" | {h['mae']:.4f} / {h['rmse']:.4f} / {h['p95']:.4f} / {h['max_abs']:.4f} |")
        raw, fin = report['scores'].get('raw', {}), report['scores'].get('final', {})
        rt, rh = raw.get('temperature') or {}, raw.get('humidity') or {}
        ft, fh = fin.get('temperature') or {}, fin.get('humidity') or {}
        imp_t = 100 * (1 - ft['mae'] / rt['mae']) if rt.get('mae') else None
        imp_h = 100 * (1 - fh['mae'] / rh['mae']) if rh.get('mae') else None
        imp = '改善（MAE）' + (f'：温度 {imp_t:.1f}%' if imp_t is not None else '') + (f' / 湿度 {imp_h:.1f}%' if imp_h is not None else '')
        lines += ['', f'raw→final {imp}。',
                  '正式门槛使用范围内未裁剪结果；非物理预测计为质量失败。',
                  '稳态/瞬态、工况、会话、温湿度分箱及覆盖详情见 metrics.json。',
                  '覆盖检查：' + ('完整' if coverage['complete'] else '；'.join(coverage['reasons'])), '',
                  'REF 标称 ±0.2°C、±2%RH；本报告仅检验相对 REF 误差，不能证明 DUT 绝对精度。',
                  '反复依据本考试结果调参会失去独立性；模型修改后应使用新的独立实验。',
                  'CSV 展示值保留一位小数，JSON 指标使用未舍入值。']
        (target / 'report.md').write_text('\n'.join(lines)+'\n')
    return report, 3 if exam and not passed else 0
