import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from linux.infer_truetemp import SNOW2S_FEATURES
from train_coupled_model import load_and_merge, prepare_features


ROOT = Path(__file__).resolve().parents[1]


class RockchipSnow2STests(unittest.TestCase):
    def test_v4_training_pairs_reference_and_preserves_brightness_and_tx_units(self):
        ref = pd.DataFrame({
            'timestamp': ['2026-11-10 10:00:00', '2026-11-10 10:00:05'],
            'temperature': [23.5, 23.5], 'humidity': [45, 45],
        })
        dut = pd.DataFrame({name: [0, 0] for name in SNOW2S_FEATURES})
        dut = dut.assign(received_at=ref.timestamp, version=[3, 4], screen_brightness=[255, 1000],
                         wifi_tx_bytes=[None, 2200])
        with patch('train_coupled_model.pd.read_excel', side_effect=[ref, dut]):
            paired = load_and_merge(['capture.xlsx'], snow2s=True)
        features, _, _, names = prepare_features(paired, snow2s=True)
        self.assertEqual(list(features.columns), names)
        self.assertEqual(features.screen_brightness.tolist(), [1000])
        self.assertEqual(features.wifi_tx_bytes.tolist(), [2200])
        with patch('train_coupled_model.pd.read_excel', side_effect=[ref, dut.drop(columns='version')]):
            with self.assertRaisesRegex(ValueError, 'DUT v4'):
                load_and_merge(['old.xlsx'], snow2s=True)

    def test_v4_python_inference_and_header_use_the_same_brightness_feature(self):
        # Artificial trees exercise branching only. No model is written to the workspace.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            brightness = SNOW2S_FEATURES.index('screen_brightness')
            temperature = {'f': brightness, 't': 255.5, 'l': {'v': 21},
                           'r': {'f': brightness, 't': 900, 'l': {'v': 22}, 'r': {'v': 23}}}
            for name, tree in [('temperature', temperature), ('vapor_pressure', {'v': 0})]:
                (path / f'model_{name}.json').write_text(json.dumps({
                    'protocol_version': 4, 'features': SNOW2S_FEATURES, 'trees': [tree],
                }))
            for value, expected in [(255, 21), (256, 22), (1000, 23)]:
                args = ['linux/infer_truetemp.py', '--snow2s', '--model-dir', directory,
                        '26', '45', '45', '30', '7200', str(value), '30', '10', '2200', '0']
                result = subprocess.run([sys.executable, '-B', *args], cwd=ROOT, check=True,
                                        text=True, capture_output=True)
                self.assertIn(f'temperature={expected:.4f}', result.stdout)
            header = path / 'model.h'
            subprocess.run([sys.executable, '-B', 'gen_c_model.py', '--linux', '--snow2s',
                            '--model-dir', directory, '--output', str(header)], cwd=ROOT, check=True,
                           capture_output=True)
            text = header.read_text()
            self.assertIn('#define TRUETEMP_MODEL_PROTOCOL_VERSION 4', text)
            self.assertIn('TRUETEMP_F_SCREEN_BRIGHTNESS = 5', text)
            self.assertIn('TRUETEMP_F_WIFI_TX_BYTES = 8', text)
            self.assertNotIn('TRUETEMP_F_C5_SOC_TEMP', text)

            result = subprocess.run([sys.executable, '-B', 'gen_c_model.py', '--linux', '--snow2s',
                                     '--model-dir', 'models', '--output', str(header)], cwd=ROOT,
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('DUT v4', result.stderr)
            self.assertEqual(header.read_text(), text)
