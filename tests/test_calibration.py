import json
import shutil
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from truetemp.artifacts import read_json, write_json
from truetemp.dataset import build_dataset, load_split
from truetemp.physics import saturation_pressure, vapor_pressure, relative_humidity
from truetemp.training import train
from truetemp.calibration import load_bundle, predict_csv, Calibration
from truetemp.evaluation import evaluate, error_metrics, coverage_check
from truetemp.cli import main


def make_session(root, name, day, count=90, offset=.5, constant=False):
    folder = root / name
    folder.mkdir()
    for role, filename, device in [('reference', 'session.json', 'ref'), ('dut', 'dut_session.json', 'dut')]:
        write_json(folder / filename, dict(status='stopped', device_id=device, interval_seconds=5))
    refs, duts = [], []
    for i in range(count):
        temp = 25 if constant else 20 + (i % 30) / 3
        humidity = 50 if constant else 40 + i % 20
        when = datetime(2026, 1, day, tzinfo=timezone.utc) + timedelta(seconds=5*i)
        common = dict(received_at=when.isoformat(), monotonic_seconds=1000+5*i, status='valid')
        raw_t = round((temp+45)/175*65535)
        raw_h = round((humidity+6)/125*65535)
        refs.append(dict(common, device_id='ref', company_id=0x06D5,
                         payload_hex=struct.pack('<BBHHH', 0, 6, 1, raw_t, raw_h).hex()))
        heat = 1 if constant else (i % 3)*.5 + .5
        payload = struct.pack('<BBhhhhHBHBBhh', 3, 0, round((temp+heat)*10), round(humidity*10), 0,
                              round((temp+2)*10), 100 if constant else i*5, 100, 0, 30, 0, 0, 0)
        duts.append(dict(common, monotonic_seconds=1000+5*i+offset, device_id='dut', company_id=0xDFDF, payload_hex=payload.hex()))
    for file, rows in [('raw.jsonl', refs), ('dut_raw.jsonl', duts)]:
        (folder/file).write_text(''.join(json.dumps(row)+'\n' for row in rows))
    return folder


def make_dataset(root, count=90, constant=False):
    for i, split in enumerate(('train', 'validation', 'test'), 1):
        make_session(root, split, i, count=count, constant=constant)
    manifest = root/'experiment.toml'
    manifest.write_text('\n'.join(f'''[[sessions]]
path = "{s}"
experiment_id = "{s}-experiment"
condition_id = "room"
phase = "steady"
split = "{s}"
''' for s in ('train', 'validation', 'test')))
    dataset = root/'dataset'
    build_dataset(manifest, dataset)
    return dataset, manifest


def test_physics_values_and_invariants():
    assert saturation_pressure(0) == pytest.approx(6.112, rel=.001)
    assert saturation_pressure(25) == pytest.approx(31.699, rel=.001)
    temperatures = np.linspace(0, 60, 100)
    assert (np.diff(relative_humidity(temperatures, 5)) < 0).all()
    assert np.allclose(relative_humidity(temperatures, vapor_pressure(temperatures, 50)), 50)
    assert relative_humidity(24, vapor_pressure(26, 50)) > 50
    assert abs(saturation_pressure(-1e-6)-saturation_pressure(1e-6)) < 1e-5
    for value in (np.nan, np.inf, -41, 126):
        with pytest.raises(ValueError):
            saturation_pressure(value)


def test_dataset_precision_pairing_and_leakage(tmp_path):
    data, manifest = make_dataset(tmp_path, count=4)
    frame, _ = load_split(data, 'train')
    assert len(frame) == 4
    assert frame.pair_gap_seconds.eq(.5).all()
    assert frame.ref_temperature.iloc[0] != round(frame.ref_temperature.iloc[0], 1)
    assert frame.sample_weight.eq(1).all()
    # Rebuilding into an existing dataset directory overwrites it in place.
    counts = build_dataset(manifest, data)
    assert counts['train'] == 4
    assert load_split(data, 'train')[0].ref_temperature.notna().all()
    manifest.write_text(manifest.read_text().replace('validation-experiment', 'train-experiment'))
    with pytest.raises(ValueError, match='实验组'):
        build_dataset(manifest, tmp_path/'bad')
    frame.to_csv(data/'train.csv', index=False, float_format='%.1f')
    with pytest.raises(ValueError, match='指纹'):
        load_split(data, 'train')


def test_alignment_gap_exclusion_and_reboot(tmp_path):
    session = make_session(tmp_path, 'one', 1, count=4, offset=2.1)
    manifest = tmp_path/'experiment.toml'
    manifest.write_text('[[sessions]]\npath="one"\nexperiment_id="one"\ncondition_id="room"\nsplit="test"\n')
    with pytest.raises(ValueError, match='没有容差'):
        build_dataset(manifest, tmp_path/'no_pairs')
    manifest.write_text(manifest.read_text()+'\n[alignment]\nmax_gap="3s"\n')
    assert build_dataset(manifest, tmp_path/'paired')['test'] == 4
    manifest.write_text('''[[sessions]]
path="one"
experiment_id="one"
condition_id="room"
split="test"
exclude=[{start=5, end=10, reason="初始化"}]
[alignment]
max_gap="3s"
''')
    assert build_dataset(manifest, tmp_path/'excluded')['test'] == 3
    quality = read_json(tmp_path/'excluded'/'quality.json')['test'][0]
    assert quality['excluded_dut'] == quality['excluded_reference'] == 1


def test_end_to_end_and_reload(tmp_path):
    data, _ = make_dataset(tmp_path)
    config = tmp_path/'training.toml'
    config.write_text('[training]\nmax_iterations=60\nearly_stopping=10\nnum_leaves=[7]\nreg_lambda=[5.0]\n')
    model = tmp_path/'model'
    model_id, _ = train(data, model, config)
    bundle = load_bundle(model)
    assert bundle.metadata['model_id'] == model_id
    assert 'test' not in bundle.metadata['dataset_files']
    assert all(s['split'] != 'test' for s in bundle.metadata['sources'])
    frame, _ = load_split(data, 'test')
    predicted = bundle.predict(frame)
    assert np.isfinite(predicted.corrected_humidity).all()
    assert 'temperature_soc' not in bundle.metadata['models']['temperature']['features']
    assert np.allclose(predicted.corrected_temperature, load_bundle(model).predict(frame).corrected_temperature)
    report, code = evaluate(data, model, tmp_path/'report', exam=True)
    # Coverage is auto-derived from the data's own envelope; the constant model has
    # zero error, so the exam passes without declaring an explicit coverage gate.
    assert code == 0 and report['passed'] and report['coverage']['complete']
    source = tmp_path/'dut.csv'
    frame.drop(columns=['ref_temperature', 'ref_humidity']).to_csv(source, index=False)
    assert predict_csv(source, model, tmp_path/'prediction.csv') == len(frame)
    with pytest.raises(FileExistsError):
        predict_csv(source, model, tmp_path/'prediction.csv')
    # Generic model is not bound to a single DUT unit: another device_id is accepted.
    other = bundle.predict(frame.assign(device_id='another-unit'))
    assert np.isfinite(other.corrected_humidity).all()
    with pytest.raises(ValueError, match='缺少'):
        bundle.predict(frame.drop(columns=['raw_temperature']))
    # Training must not open the test CSV, even when unavailable.
    (data/'test.csv').unlink()
    train(data, tmp_path/'another-model', config)


def test_constant_fallback_and_no_leakage(tmp_path):
    data, _ = make_dataset(tmp_path, count=4, constant=True)
    model = tmp_path/'model'
    _, warnings = train(data, model)
    assert warnings and load_bundle(model).metadata['models']['temperature']['constant'] is not None
    meta = read_json(data/'manifest.json')
    # Relabeling development samples as test cannot evade source checks.
    test_source = next(s for s in meta['sources'] if s['split'] == 'test')
    test_source['experiment_id'] = 'train-experiment'
    write_json(data/'manifest.json', meta)
    with pytest.raises(ValueError, match='实验组'):
        evaluate(data, model, tmp_path/'leaked')


def test_candidate_quality_and_metrics(tmp_path):
    data, _ = make_dataset(tmp_path, count=4, constant=True)
    train(data, tmp_path/'model')
    bundle = load_bundle(tmp_path/'model')
    frame, _ = load_split(data, 'test')
    bundle.metadata['models']['vapor_pressure']['constant'] = -100
    predicted = bundle.predict(frame)
    assert predicted.humidity_candidate.lt(0).all()
    assert predicted.humidity_unclipped.eq(0).all()
    assert predicted.quality_flags.str.contains('negative_vapor_pressure').all()
    metric = error_metrics([0, 0], [-1, 2], 1)
    assert metric['mae'] == 1.5 and metric['bias'] == .5 and metric['pass_fraction'] == .5
    assert not coverage_check(predicted, {})['complete']


def test_derived_quantities_and_rich_metrics(tmp_path):
    """物理派生量（露点/绝对湿度）写入推理，误差指标含 R²/P50/P95/P99。"""
    data, _ = make_dataset(tmp_path, count=20)
    train(data, tmp_path/'model')
    bundle = load_bundle(tmp_path/'model')
    frame, _ = load_split(data, 'test')
    predicted = bundle.predict(frame)
    assert {'dew_point', 'absolute_humidity'} <= set(predicted.columns)
    assert np.isfinite(predicted.absolute_humidity).all()
    assert np.isfinite(predicted.dew_point.dropna()).all()
    metric = error_metrics([0, 0, 1], [0, .3, 1], .3)
    assert metric['r2'] is not None
    for key in ('p50', 'p95', 'p99'):
        assert np.isfinite(metric[key])
    assert metric['p50'] <= metric['p95'] <= metric['p99'] <= metric['max_abs']


def test_cli_pipeline_and_error_codes(tmp_path):
    data, _ = make_dataset(tmp_path, count=4, constant=True)
    model, report = tmp_path/'model', tmp_path/'report'
    assert main(['train', str(data), '--output', str(model)]) == 0
    assert main(['evaluate', str(data), '--model', str(model), '--output', str(report), '--exam']) == 0
    # Retraining into an existing model directory overwrites it in place.
    assert main(['train', str(data), '--output', str(model)]) == 0


def test_copied_source_and_time_overlap_rejected(tmp_path):
    _, manifest = make_dataset(tmp_path, count=4)
    shutil.copyfile(tmp_path/'train'/'raw.jsonl', tmp_path/'validation'/'raw.jsonl')
    with pytest.raises(ValueError, match='日志跨集合重复'):
        build_dataset(manifest, tmp_path/'copied')
    shutil.rmtree(tmp_path/'validation')
    make_session(tmp_path, 'validation', 1, count=5)
    with pytest.raises(ValueError, match='时间跨集合重叠'):
        build_dataset(manifest, tmp_path/'overlap')


def test_reboot_prevents_cross_boundary_pair(tmp_path):
    folder = make_session(tmp_path, 'one', 1, count=3, offset=.5)
    rows = [json.loads(line) for line in (folder/'dut_raw.jsonl').read_text().splitlines()]
    for i, row in enumerate(rows):
        payload = bytearray.fromhex(row['payload_hex'])
        struct.pack_into('<H', payload, 10, [100, 105, 0][i])
        row['payload_hex'] = payload.hex()
    (folder/'dut_raw.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    manifest = tmp_path/'manifest.toml'
    manifest.write_text('[[sessions]]\npath="one"\nexperiment_id="one"\ncondition_id="room"\nsplit="test"\n')
    assert build_dataset(manifest, tmp_path/'dataset')['test'] == 2
    assert read_json(tmp_path/'dataset'/'quality.json')['test'][0]['reboot_count'] == 1


def test_coverage_gate_and_exam_success(tmp_path):
    data, manifest = make_dataset(tmp_path, count=4, constant=True)
    manifest.write_text(manifest.read_text()+'''\n[coverage]
temperature_range=[0,60]
humidity_range=[0,100]
temperature_bin_width=70
humidity_bin_width=100
conditions=["room"]
min_samples_per_cell=2
''')
    covered = tmp_path/'covered'
    build_dataset(manifest, covered)
    model = tmp_path/'model'
    train(covered, model)
    report, code = evaluate(covered, model, tmp_path/'report', exam=True)
    assert code == 0 and report['passed']
    assert report['coverage']['cells'][0]['count'] == 4
    # This intentionally coarse synthetic declaration tests the gate, not accuracy evidence.
    bundle = load_bundle(model)
    test, _ = load_split(covered, 'test')
    predicted = bundle.predict(test)
    predicted['phase'] = 'transient'
    assert not coverage_check(predicted, bundle.metadata['coverage'])['complete']


def test_training_really_saves_native_trees(tmp_path):
    data, _ = make_dataset(tmp_path, count=240)
    config = tmp_path/'config.toml'
    config.write_text('[training]\nmax_iterations=100\nearly_stopping=10\nnum_leaves=[7]\nreg_lambda=[5.0]\n')
    model = tmp_path/'model'
    train(data, model, config)
    bundle = load_bundle(model)
    assert (model/'temperature.txt').is_file()
    assert bundle.metadata['models']['temperature']['constant'] is None
    (model/'temperature.txt').write_text('broken')
    with pytest.raises(ValueError, match='指纹'):
        load_bundle(model)
