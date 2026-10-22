import asyncio
import csv
import io
import json
import struct
from types import SimpleNamespace

import pytest
from rich.console import Console

from truetemp.ble import Device, collect, scan
from truetemp.cli import main, select_devices
from truetemp.config import read_config, save_devices
from truetemp.export import export_csv
from truetemp.protocol import decode_dut, decode, DUT_COMPANY_ID, COMPANY_ID

PAYLOAD = struct.pack("<BBhhhhHBHBBhh", 3, 0, 266, 328, 0, 285, 152, 255, 0, 41, 0, 0, 310)
REF = SimpleNamespace(address="REF-UUID", name="SHT40 Gadget")
DUT = SimpleNamespace(address="DUT-UUID", name="Qingping Monitor Lite2")
REF_ADV = SimpleNamespace(manufacturer_data={COMPANY_ID: bytes.fromhex("00065e06e36aef71")}, local_name=REF.name, rssi=-50)
DUT_ADV = SimpleNamespace(manufacturer_data={DUT_COMPANY_ID: PAYLOAD}, local_name=DUT.name, rssi=-44)


def test_real_dut_packet():
    r = decode_dut(PAYLOAD)
    assert (r.version, r.flags) == (3, 0)
    assert (r.raw_temperature, r.raw_humidity, r.temperature_soc, r.temperature_co2) == (26.6, 32.8, 0, 28.5)
    assert (r.time_uptime, r.screen_brightness, r.screen_on_duration_s) == (152, 255, 0)
    assert (r.cpu_load_10s, r.wifi_load_10s, r.power_charge_ma) == (41, 0, 0)
    assert r.c5_soc_temp == 31.0


def test_signed_fields_and_limits():
    r = decode_dut(struct.pack("<BBhhhhHBHBBhh", 3, 1, -100, 1000, -20, -30, 7200, 255, 3600, 100, 100, -1234, -50))
    assert r.raw_temperature == -10 and r.power_charge_ma == -1234
    assert r.flags == 1 and r.time_uptime == 7200 and r.c5_soc_temp == -5.0


@pytest.mark.parametrize('payload', [PAYLOAD[:-1], PAYLOAD+b'\0',
    struct.pack("<BBhhhhHBHBBh", 3, 0, 200, 500, 0, 200, 0, 0, 0, 0, 0, 0),  # legacy 19B
    struct.pack("<BBhhhhHBHBBhh", 4, 0, 200, 500, 0, 200, 0, 0, 0, 0, 0, 0, 0),  # wrong version
    struct.pack("<BBhhhhHBHBBhh", 3, 0, 200, 500, 0, 200, 7201, 0, 0, 0, 0, 0, 0),
    struct.pack("<BBhhhhHBHBBhh", 3, 0, 200, 500, 0, 200, 0, 0, 3601, 0, 0, 0, 0),
    struct.pack("<BBhhhhHBHBBhh", 3, 0, 200, 500, 0, 200, 0, 0, 0, 101, 0, 0, 0),
    struct.pack("<BBhhhhHBHBBhh", 3, 0, 200, 500, 0, 200, 0, 0, 0, 0, 101, 0, 0)])
def test_invalid_protocol(payload):
    with pytest.raises(ValueError):
        decode_dut(payload)


class MixedScanner:
    def __init__(self, detection_callback):
        self.callback = detection_callback

    async def start(self):
        self.callback(REF, REF_ADV)
        self.callback(DUT, DUT_ADV)
        self.callback(DUT, DUT_ADV)
        self.callback(SimpleNamespace(address="UNSELECTED"), DUT_ADV)

    async def stop(self):
        pass


def test_atomic_selection_role_checks_and_cancel(tmp_path):
    path = tmp_path / 'config.toml'
    save_devices(path, {'reference': REF.address})
    entries = [Device(REF.address, REF.name, -50, 0, decode(REF_ADV.manufacturer_data[COMPANY_ID]), None),
               Device(DUT.address, DUT.name, -44, 0, decode_dut(PAYLOAD), None, 'dut')]
    answers = iter(['1', 'q'])
    select_devices(entries, path, Console(file=io.StringIO()), ask=lambda _: next(answers))
    assert 'dut' not in read_config(path)['devices']
    answers = iter(['2', '1', '1', '2'])  # reject wrong role at each prompt
    select_devices(entries, path, Console(file=io.StringIO()), ask=lambda _: next(answers))
    assert read_config(path)['devices']['dut'] == DUT.address
    original = path.read_bytes()
    with pytest.raises(ValueError):
        save_devices(path, {'dut': REF.address.lower()})
    assert path.read_bytes() == original


def test_dual_collect_and_export(tmp_path):
    session = tmp_path / 'session'
    async def run():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(.03, stop.set)
        await collect(session, REF.address, .01, 'test', Console(file=io.StringIO()),
                      stop=stop, scanner_factory=MixedScanner, dut=DUT.address)
    asyncio.run(run())
    ref_rows = list(csv.DictReader((session/'reference.csv').open()))
    dut_rows = list(csv.DictReader((session/'dut.csv').open()))
    assert len(ref_rows) == len(dut_rows) == 1
    assert dut_rows[0]['raw_temperature'] == '26.6'
    assert dut_rows[0]['temperature_soc'] == '0.0'
    assert dut_rows[0]['device_id'] == DUT.address
    assert 'monotonic_seconds' not in dut_rows[0]
    raw = [json.loads(line) for line in (session/'dut_raw.jsonl').read_text().splitlines()]
    assert len(raw) == 2 and raw[0]['company_id'] == DUT_COMPANY_ID
    assert json.loads((session/'dut_session.json').read_text())['status'] == 'stopped'
    output, count = export_csv(session, device='dut')
    assert output.name == 'session_dut.csv' and count == 1
    with output.open(encoding='utf-8-sig') as f:
        assert list(csv.DictReader(f)) == dut_rows


def test_cli_dut_setting(tmp_path):
    path = tmp_path/'config.toml'
    assert main(['device', 'set', 'dut', DUT.address, '--config', str(path)]) == 0
    assert read_config(path)['devices']['dut'] == DUT.address
