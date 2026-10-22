import asyncio
import csv
import io
import json
import struct
from types import SimpleNamespace

import pytest
from rich.console import Console

from truetemp.ble import Recorder, collect, scan, Device
from truetemp.cli import main, select_reference
from truetemp.config import read_config, save_reference
from truetemp.protocol import decode, COMPANY_ID

# Hand-calculated wire vector: T=-10 C -> raw=13107; RH=44% -> raw=26214.
PAYLOAD = bytes.fromhex("0006341233336666")
DEV = SimpleNamespace(address="REF-UUID", name="SHT4x Gadget")
ADV = SimpleNamespace(manufacturer_data={COMPANY_ID: PAYLOAD}, local_name="SHT4x Gadget", rssi=-50)


def test_wire_decode_and_negative_temperature():
    reading = decode(PAYLOAD)
    assert reading.gadget_id == 0x1234
    assert reading.temperature == pytest.approx(-10)
    assert reading.humidity == pytest.approx(44)
    assert decode(bytes.fromhex("0006341299996666")).temperature == pytest.approx(60)


@pytest.mark.parametrize("payload", [b"", PAYLOAD[:-1], PAYLOAD + b"\0", b"\0\4" + PAYLOAD[2:], struct.pack("<BBHHH", 0, 6, 1, 13107, 65535)])
def test_reject_invalid(payload):
    with pytest.raises(ValueError):
        decode(payload)


def test_config_preserves_other_fields_and_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# keep\n[collection]\ninterval = "2s"\n')
    save_reference(path, DEV.address)
    assert read_config(path)["devices"]["reference"] == DEV.address
    assert '# keep' in path.read_text()
    assert read_config(path)["collection"]["interval"] == "2s"


def test_recorder_keeps_identical_receipts_and_does_not_fill_gaps(tmp_path):
    folder = tmp_path / "session"
    r = Recorder(folder, DEV.address, 2, "test")
    r.receive(DEV, ADV)
    r.flush_sample()
    r.flush_sample()  # no callback: must not reuse the old sample
    r.receive(DEV, ADV)  # identical value is not evidence of no new measurement
    r.flush_sample()
    r.receive(SimpleNamespace(address="OTHER"), ADV)
    r.receive(DEV, SimpleNamespace(manufacturer_data={COMPANY_ID: b"bad"}, rssi=-60))
    r.close("stopped")
    rows = list(csv.DictReader((folder / "reference.csv").open()))
    assert len(rows) == 2
    assert "monotonic_seconds" not in rows[0]
    assert "." not in rows[0]["received_at"]
    assert rows[0]["received_at"].endswith("+00:00")
    assert rows[0]["temperature"] == "-10.0"
    assert rows[0]["humidity"] == "44.0"
    raw = [json.loads(line) for line in (folder / "raw.jsonl").read_text().splitlines()]
    assert len(raw) == 3 and raw[-1]["status"] == "invalid"
    assert "monotonic_seconds" in raw[0]
    assert json.loads((folder / "session.json").read_text())["invalid_packets"] == 1
    with pytest.raises(FileExistsError):
        Recorder(folder, DEV.address, 2, "")


class FakeScanner:
    stopped = False

    def __init__(self, detection_callback):
        self.callback = detection_callback

    async def start(self):
        self.callback(DEV, ADV)
        self.callback(DEV, ADV)

    async def stop(self):
        self.stopped = True


def test_scan_deduplicates_and_selection_saves(tmp_path):
    console = Console(file=io.StringIO())
    devices = asyncio.run(scan(.01, None, console, scanner_factory=FakeScanner))
    assert len(devices) == 1
    path = tmp_path / "config.toml"
    answers = iter(["99", "1"])
    select_reference(devices, None, path, console, ask=lambda _: next(answers))
    assert read_config(path)["devices"]["reference"] == DEV.address


def test_cancel_selection_does_not_write(tmp_path):
    path = tmp_path / "config.toml"
    devices = [Device(DEV.address, DEV.name, -50, 0, decode(PAYLOAD), None)]
    select_reference(devices, None, path, Console(file=io.StringIO()), ask=lambda _: "q")
    assert not path.exists()


def test_collect_clean_stop(tmp_path):
    async def run():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(.03, stop.set)
        await collect(tmp_path / "s", DEV.address, .01, "", Console(file=io.StringIO()),
                      stop=stop, scanner_factory=FakeScanner)
    asyncio.run(run())
    meta = json.loads((tmp_path / "s/session.json").read_text())
    assert meta["status"] == "stopped" and meta["samples"] == 1


def test_scanner_failure_preserves_session(tmp_path):
    class FailedScanner(FakeScanner):
        async def start(self):
            raise OSError("adapter unavailable")
    with pytest.raises(OSError):
        asyncio.run(collect(tmp_path / "s", DEV.address, 2, "", Console(file=io.StringIO()),
                            scanner_factory=FailedScanner))
    assert json.loads((tmp_path / "s/session.json").read_text())["status"] == "failed"


def test_missing_reference_is_actionable(tmp_path):
    assert main(["collect", "--config", str(tmp_path / "missing.toml")]) == 2


def test_real_sht40_gadget_advertisement():
    # Received locally from SHT40 Gadget; company ID was 0x06D5.
    reading = decode(bytes.fromhex("00065e06e36aef71"))
    assert reading.gadget_id == 1630
    assert reading.temperature_raw == 27363
    assert reading.humidity_raw == 29167
    assert reading.temperature == pytest.approx(28.06820782787824)
    assert reading.humidity == pytest.approx(49.632486457618064)
