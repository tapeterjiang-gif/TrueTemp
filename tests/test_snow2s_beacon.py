import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree
from zipfile import ZipFile

from collect_data import Recorder, write_xlsx
from truetemp.protocol import DUT_COMPANY_ID, decode_dut


def snow2s_packet(brightness, flags=0x80):
    return struct.pack(
        "<BBHhhhhHHHHHbHHH6s",
        4, flags, 42, -123, 456, 510, 318, 7000, brightness, 4321, 413, 0,
        -44, 2000, 850, 4150, b"\x01\x02\x03\x04\x05\x06",
    )


class Snow2SBeaconTests(unittest.TestCase):
    def test_full_brightness_range_and_optional_fields(self):
        for brightness in (0, 255, 256, 1000):
            with self.subTest(brightness=brightness):
                reading = decode_dut(snow2s_packet(brightness))
                self.assertEqual(reading.screen_brightness, brightness)
                self.assertEqual(reading.cpu_load_10s, 41.3)
                self.assertEqual(reading.wifi_tx_bytes, 2000)
                self.assertEqual(reading.ambient_lux, 0)
                self.assertIsNone(reading.wifi_load_10s)
        self.assertIsNone(decode_dut(snow2s_packet(1000, flags=0)).ambient_lux)
        for malformed in (snow2s_packet(1001), snow2s_packet(1000)[:-1]):
            with self.assertRaises(ValueError):
                decode_dut(malformed)

    def test_collect_keeps_v3_and_writes_v4_raw_brightness_to_xlsx(self):
        recorder = Recorder("dut", "device", 5)
        lite2 = struct.pack("<BBhhhhHBHBBhh", 3, 0, 235, 456, 510, 318, 152, 255, 0, 41, 0, 0, 310)
        for packet in (lite2, snow2s_packet(1000, flags=0)):
            recorder.receive("device", SimpleNamespace(manufacturer_data={DUT_COMPANY_ID: packet}))
            recorder.flush_sample()
        self.assertIn("亮 1000", recorder.status_line())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.xlsx"
            write_xlsx(output, [], recorder.samples)
            with ZipFile(output) as workbook:
                root = ElementTree.fromstring(workbook.read("xl/worksheets/sheet2.xml"))
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        self.assertEqual(root.find(".//s:c[@r='G2']/s:v", ns).text, "255")
        self.assertEqual(root.find(".//s:c[@r='G3']/s:v", ns).text, "1000")
        self.assertEqual(root.find(".//s:c[@r='M3']/s:v", ns).text, "4")
        self.assertIsNone(root.find(".//s:c[@r='O3']/s:v", ns))
