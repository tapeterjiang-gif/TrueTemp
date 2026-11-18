import ctypes
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

import pandas as pd

from gen_c_model import gen_linux_data
from linux.infer_truetemp import (
    DELTA_GATE_STRATEGY, SNOW2S_FEATURES, SNOW2S_DELTA_FEATURES, SNOW2S_HEAT_FEATURES,
    compensate, load_temperature_policy,
)
from train_snow2s_hybrid import replay_pairs


ROOT = Path(__file__).resolve().parents[1]


def branch(features, trees):
    return {'protocol_version': 4, 'features': list(features), 'trees': trees}


def policy():
    return {
        'strategy': DELTA_GATE_STRATEGY, 'raw_temperature_max': 26,
        'delta_median': 2.6, 'heat_gate_max': 3.5, 'shrink_fraction': .75,
        'delta_model': branch(SNOW2S_DELTA_FEATURES, [{
            'f': SNOW2S_DELTA_FEATURES.index('temperature_co2'), 't': 30,
            'l': {'v': 2}, 'r': {'v': 4},
        }]),
        'heat_model': branch(SNOW2S_HEAT_FEATURES, [{
            'f': SNOW2S_HEAT_FEATURES.index('screen_brightness'), 't': 500,
            'l': {'v': 3.5}, 'r': {'v': 3.6},
        }]),
    }


def write_models(path, temperature_policy):
    temperature = branch(SNOW2S_FEATURES, [{'v': 21}])
    if temperature_policy is not None:
        temperature['temperature_policy'] = temperature_policy
    (path / 'model_temperature.json').write_text(json.dumps(temperature))
    (path / 'model_vapor_pressure.json').write_text(json.dumps(branch(SNOW2S_FEATURES, [{'v': 0}])))


class Inputs(ctypes.Structure):
    _fields_ = [(name, ctypes.c_float) for name in SNOW2S_FEATURES]


class Outputs(ctypes.Structure):
    _fields_ = [(name, ctypes.c_float) for name in ('temperature', 'humidity', 'dew_point', 'absolute_humidity')]


def compile_runtime(path, *, compact_nodes=True):
    for filename in ('truetemp.c', 'truetemp.h'):
        shutil.copyfile(ROOT / 'linux' / filename, path / filename)
    gen_linux_data(str(path / 'model_temperature.json'), str(path / 'model_vapor_pressure.json'),
                   str(path / 'truetemp_snow2s_model_data.h'), snow2s=True, compact_nodes=compact_nodes)
    source = path / 'runtime_size_check.c'
    node_bytes = 8 if compact_nodes else 12
    source.write_text('#include "truetemp.c"\n'
                      f'typedef char node_size_check[(sizeof(truetemp_node_t) == {node_bytes}) ? 1 : -1];\n')
    library = path / 'libtruetemp.so'
    subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-std=c99', '-Wall', '-Wextra', '-Werror',
                    '-DTRUETEMP_SNOW2S', str(source), '-lm', '-o', str(library)],
                   check=True, capture_output=True)
    loaded = ctypes.CDLL(str(library))
    loaded.truetemp_compensate_snow2s.argtypes = [ctypes.POINTER(Inputs), ctypes.POINTER(Outputs)]
    loaded.truetemp_compensate_snow2s.restype = None
    return loaded


def infer_c(library, values):
    output = Outputs()
    library.truetemp_compensate_snow2s(ctypes.byref(Inputs(*values)), ctypes.byref(output))
    return [getattr(output, name) for name, _ in Outputs._fields_]


class Snow2SHybridTests(unittest.TestCase):
    def test_compact_and_legacy_nodes_produce_identical_float_bits_at_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            libraries = []
            for compact in (False, True):
                path = Path(directory) / ('compact' if compact else 'legacy')
                path.mkdir()
                write_models(path, policy())
                libraries.append(compile_runtime(path, compact_nodes=compact))
            for raw in (26, 26.001, 27, 50):
                for co2 in (29.999, 30, 30.001):
                    for brightness in (499.999, 500, 500.001):
                        values = [raw, 40, 45, co2, 300, brightness, 100, 10, 200, 0]
                        outputs = [struct.pack('4f', *infer_c(lib, values)) for lib in libraries]
                        self.assertEqual(outputs[0], outputs[1])

    def test_compact_absolute_model_rejects_an_old_node_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_models(path, None)
            gen_linux_data(str(path / 'model_temperature.json'), str(path / 'model_vapor_pressure.json'),
                           str(path / 'truetemp_snow2s_model_data.h'), snow2s=True)
            source = path / 'old_runtime.c'
            source.write_text('#include "truetemp_snow2s_model_data.h"\n')
            result = subprocess.run(['cc', '-fsyntax-only', str(source)], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('requires the compact-node runtime', result.stderr)

    def test_c_and_python_switch_boundaries_and_humidity_follow_final_temperature(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_models(path, policy())
            loaded = load_temperature_policy(path / 'model_temperature.json', snow2s=True)
            library = compile_runtime(path)
            cases = [
                (20, 35, 500, 21),       # Lower temperatures keep the absolute model.
                (26, 35, 500, 21),       # At the upper bound the original model still applies.
                (26.001, 35, 501, 22.001),
                (27, 35, 500, 24.05),    # Equal to the heat gate: 4 - .75 * (4 - 2.6).
                (27, 35, 501, 23),       # Strong heat: subtract the whole 4 C.
                (27, 20, 500, 25),       # Delta below median must not be increased.
                (50, 35, 501, 46),       # Raw temperature is added only after delta prediction.
            ]
            for raw, co2, brightness, expected in cases:
                with self.subTest(raw=raw, co2=co2, brightness=brightness):
                    values = [raw, 40, 45, co2, 300, brightness, 100, 10, 200, 0]
                    python = compensate(values, [{'v': 21}], [{'v': 0}], loaded)
                    actual = infer_c(library, values)
                    self.assertAlmostEqual(python[0], expected, places=5)
                    for result, reference in zip(actual, python):
                        self.assertAlmostEqual(result, reference, delta=.0002)
            bad = [27, 40, float('nan'), 35, 300, 500, 100, 10, 200, 0]
            self.assertTrue(all(map(math.isnan, infer_c(library, bad))))
            with self.assertRaisesRegex(ValueError, 'finite'):
                compensate(bad, [{'v': 21}], [{'v': 0}], loaded)
            args = [sys.executable, '-B', str(ROOT / 'linux/infer_truetemp.py'), '--snow2s',
                    '--model-dir', str(path), '27', '40', '45', '35', '300', '500', '100', '10', '200', '0']
            self.assertIn('temperature=24.0500', subprocess.check_output(args, text=True))

    def test_all_auxiliary_feature_indices_map_to_the_c_input_layout(self):
        for key, features in [('delta_model', SNOW2S_DELTA_FEATURES), ('heat_model', SNOW2S_HEAT_FEATURES)]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                current = policy()
                current['delta_model']['trees'] = [{'v': 4}]
                current['heat_model']['trees'] = [{'v': 0}]
                # Distinct split values exercise the compact model's indices independently.
                current[key]['trees'] = [
                    {'f': i, 't': 10 + i, 'l': {'v': 0}, 'r': {'v': 4 + i}}
                    for i in range(len(features))
                ]
                write_models(path, current)
                library = compile_runtime(path)
                for name in features:
                    values = [0.] * 10
                    values[0] = 27
                    values[SNOW2S_FEATURES.index(name)] = 100
                    expected = compensate(values, [{'v': 21}], [{'v': 0}], current)[0]
                    self.assertAlmostEqual(infer_c(library, values)[0], expected, places=4)

    def test_gate_45_and_full_shrink_keep_small_deltas_and_respect_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            current = policy()
            current.update(heat_gate_max=4.5, shrink_fraction=1.0)
            current['heat_model']['trees'][0]['l']['v'] = 4.5
            current['heat_model']['trees'][0]['r']['v'] = 4.6
            write_models(path, current)
            loaded = load_temperature_policy(path / 'model_temperature.json', snow2s=True)
            library = compile_runtime(path)
            for raw, co2, brightness, expected in [
                (26, 35, 500, 21),       # At the raw upper bound, keep the absolute model.
                (27, 35, 500, 24.4),     # Heat exactly at 4.5: cap the delta at 2.6.
                (27, 35, 501, 23),       # Heat above 4.5: keep the whole predicted delta.
                (27, 20, 500, 25),       # A predicted delta below 2.6 must stay unchanged.
            ]:
                with self.subTest(raw=raw, co2=co2, brightness=brightness):
                    values = [raw, 40, 45, co2, 300, brightness, 100, 10, 200, 0]
                    python = compensate(values, [{'v': 21}], [{'v': 0}], loaded)
                    self.assertAlmostEqual(python[0], expected, places=5)
                    for actual, reference in zip(infer_c(library, values), python):
                        self.assertAlmostEqual(actual, reference, delta=.0002)

    def test_legacy_model_and_policy_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_models(path, None)
            self.assertIsNone(load_temperature_policy(path / 'model_temperature.json', snow2s=True))
            library = compile_runtime(path)
            self.assertEqual(infer_c(library, [50, 40, 45, 35, 300, 500, 100, 10, 200, 0])[0], 21)
            for field, value in [('raw_temperature_max', float('nan')), ('shrink_fraction', 2)]:
                broken = policy()
                broken[field] = value
                write_models(path, broken)
                with self.assertRaises(ValueError):
                    load_temperature_policy(path / 'model_temperature.json', snow2s=True)
            broken = policy()
            broken['delta_model']['features'][0] = 'raw_temperature'
            write_models(path, broken)
            with self.assertRaisesRegex(ValueError, 'features'):
                load_temperature_policy(path / 'model_temperature.json', snow2s=True)

    def test_new_header_rejects_an_absolute_only_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_models(path, policy())
            gen_linux_data(str(path / 'model_temperature.json'), str(path / 'model_vapor_pressure.json'),
                           str(path / 'truetemp_snow2s_model_data.h'), snow2s=True)
            source = path / 'old_runtime.c'
            source.write_text('#include "truetemp_snow2s_model_data.h"\n')
            result = subprocess.run(['cc', '-fsyntax-only', str(source)], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('requires the Snow2S delta-gate', result.stderr)

    def test_make_switches_models_without_reusing_previous_object(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            linux = path / 'linux'
            linux.mkdir()
            shutil.copyfile(ROOT / 'gen_c_model.py', path / 'gen_c_model.py')
            for name in ('Makefile', 'truetemp.c', 'truetemp.h', 'truetemp_infer.c', 'infer_truetemp.py'):
                shutil.copyfile(ROOT / 'linux' / name, linux / name)
            for name, configuration in [('legacy', None), ('hybrid', policy()), ('legacy_again', None)]:
                model_dir = path / name
                model_dir.mkdir()
                write_models(model_dir, configuration)
                subprocess.run(['make', '-C', str(linux), 'snow2s', f'SNOW2S_MODEL_DIR={model_dir}'],
                               check=True, capture_output=True)
                output = subprocess.check_output([str(linux / 'truetemp_infer_snow2s'),
                    '27', '40', '45', '35', '300', '500', '100', '10', '200', '0'], text=True)
                expected = 21 if configuration is None else 24.05
                self.assertIn(f'temperature={expected:.4f}', output)

    def test_saved_pairs_ignore_new_rows_and_reject_missing_or_ambiguous_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            stamps = ['2026-11-11 10:00:00+08:00', '2026-11-11 10:00:10+08:00']
            ref = pd.DataFrame({'timestamp': stamps, 'temperature': [21., 22.], 'humidity': [40., 40.]})
            dut = pd.DataFrame({name: [0., 0.] for name in SNOW2S_FEATURES})
            dut = dut.assign(received_at=stamps, version=4, raw_temperature=[24., 25.])
            pairs = pd.DataFrame({'source_file': ['capture.xlsx'], 'pair_method': ['reception'],
                                  'timestamp': [stamps[0]], 'received_at': [stamps[0]], 'pair_delta_s': [0.]})
            pairs.to_csv(path / 'pairs.csv', index=False)

            def workbook(reference):
                with pd.ExcelWriter(path / 'capture.xlsx') as writer:
                    reference.to_excel(writer, sheet_name='REF_RAW', index=False)
                    dut.to_excel(writer, sheet_name='DUT_RAW', index=False)

            workbook(pd.concat([ref, ref.iloc[:1]]))
            replayed, _ = replay_pairs(path / 'pairs.csv', path)
            self.assertEqual(replayed.temperature.tolist(), [21.])
            self.assertEqual(replayed.raw_temperature.tolist(), [24.])
            workbook(ref.iloc[1:])
            with self.assertRaisesRegex(ValueError, '无法完整恢复'):
                replay_pairs(path / 'pairs.csv', path)
            workbook(pd.concat([ref, ref.iloc[:1].assign(temperature=23.)]))
            with self.assertRaisesRegex(ValueError, '不同读数'):
                replay_pairs(path / 'pairs.csv', path)
