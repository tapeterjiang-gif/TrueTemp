import asyncio
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
import signal
import time

from bleak import BleakScanner
from rich.live import Live
from rich.table import Table
from rich.console import Group
from rich.text import Text

from .protocol import COMPANY_ID, DUT_COMPANY_ID, DUT_DECIMAL_FIELDS, decode, decode_dut, Reading, DutReading


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def stop_on_interrupt(event):
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    loop.add_signal_handler(signal.SIGINT, event.set)
    try:
        yield
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        signal.signal(signal.SIGINT, previous)


@dataclass
class Device:
    address: str
    name: str
    rssi: int
    seen: float
    reading: Reading | DutReading | None
    error: str | None
    role: str = "reference"


def table(devices, reference, full=False, dut=None):
    result = Table(title="TrueTemp · REF / DUT 扫描")
    for column in ("编号", "设备名称", "类型", "信号", "温度", "湿度", "当前设置"):
        result.add_column(column)
    if full:
        result.add_column("设备标识", overflow="fold")
    for i, device in enumerate(devices.values(), 1):
        reading = device.reading
        stale = "（过期）" if time.monotonic() - device.seen > 10 else ""
        row = [str(i), device.name, ("SHT4x REF" if device.role == "reference" else "DUT v3") if reading else "未知/无效",
               f"{device.rssi} dBm", f"{reading.temperature:.1f} °C{stale}" if reading else "—",
               f"{reading.humidity:.1f} %RH" if reading else "—",
               "标准表" if device.address.casefold() == (reference or "").casefold() else ("DUT" if device.address.casefold() == (dut or "").casefold() else "—")]
        if full:
            row.append(device.address)
        result.add_row(*(Text(value) for value in row))
    return result


async def scan(seconds, reference, console, stop=None, scanner_factory=BleakScanner, dut=None):
    devices = {}
    stop = stop or asyncio.Event()

    def received(device, adv):
        if DUT_COMPANY_ID in adv.manufacturer_data:
            role, company, decoder = "dut", DUT_COMPANY_ID, decode_dut
        elif COMPANY_ID in adv.manufacturer_data:
            role, company, decoder = "reference", COMPANY_ID, decode
        else:
            return
        try:
            reading, error = decoder(adv.manufacturer_data[company]), None
        except ValueError as exc:
            reading, error = None, str(exc)
        previous = devices.get(device.address)
        name = adv.local_name or device.name or (previous.name if previous else "—")
        devices[device.address] = Device(device.address, name, adv.rssi, time.monotonic(), reading, error, role)

    scanner = scanner_factory(detection_callback=received)
    with stop_on_interrupt(stop):
        try:
            await asyncio.wait_for(scanner.start(), 15)
            started = time.monotonic()
            with (Live(console=console, refresh_per_second=4, transient=True) if console.is_terminal else nullcontext()) as live:
                while not stop.is_set() and time.monotonic() - started < seconds:
                    if live:
                        live.update(Group(table(devices, reference, dut=dut), Text(
                            f"扫描中 · {time.monotonic() - started:.0f}/{seconds:g} 秒 · Ctrl+C 进入选择")))
                    await asyncio.sleep(.1)
        finally:
            await scanner.stop()
    console.print(table(devices, reference, full=True, dut=dut))
    return list(devices.values())


class Recorder:
    """Write all received REF payloads; sample latest receipt once per interval."""
    def __init__(self, output: Path, reference: str, interval: float, note: str, *, role="reference", shared_directory=False):
        output.mkdir(parents=True, exist_ok=shared_directory)
        self.role = role
        self.company = DUT_COMPANY_ID if role == "dut" else COMPANY_ID
        self.decoder = decode_dut if role == "dut" else decode
        self.decimals = DUT_DECIMAL_FIELDS if role == "dut" else ("temperature", "humidity")
        self.meta_name = "dut_session.json" if role == "dut" else "session.json"
        self.output, self.reference, self.interval = output, reference, interval
        self.raw = (output / ("dut_raw.jsonl" if role == "dut" else "raw.jsonl")).open("x", buffering=1)
        self.file = (output / f"{role}.csv").open("x", newline="", buffering=1)
        fields = list(DutReading.__dataclass_fields__) if role == "dut" else [
            "gadget_id", "temperature_raw", "humidity_raw", "temperature", "humidity"]
        self.writer = csv.DictWriter(self.file, fieldnames=["received_at", "device_id", "rssi", *fields])
        self.writer.writeheader()
        self.pending = None
        self.count = self.packets = self.invalid = 0
        self.last_valid = None
        self.last_reading = None
        self.meta = dict(schema_version=2, role=role, device_id=reference, reference=reference if role == "reference" else None, interval_seconds=interval,
                         note=note, started_at=utc(), status="running")
        self.write_meta()

    def write_meta(self):
        temporary = self.output / f".{self.meta_name}.tmp"
        temporary.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2))
        temporary.replace(self.output / self.meta_name)

    def receive(self, device, adv):
        if device.address.casefold() != self.reference.casefold():
            return
        payload = adv.manufacturer_data.get(self.company)
        if payload is None:
            return
        now, tick = utc(), time.monotonic()
        self.packets += 1
        event = dict(received_at=now, monotonic_seconds=tick, device_id=device.address,
                     rssi=adv.rssi, company_id=self.company, role=self.role, payload_hex=payload.hex())
        try:
            reading = self.decoder(payload)
            self.pending = dict(received_at=now, device_id=device.address,
                                rssi=adv.rssi, **asdict(reading))
            self.last_valid = tick
            self.last_reading = reading
            event.update(status="valid", **asdict(reading))
        except ValueError as exc:
            self.invalid += 1
            event.update(status="invalid", error=str(exc))
        self.raw.write(json.dumps(event, ensure_ascii=False) + "\n")

    def flush_sample(self):
        if self.pending is not None:
            row = dict(self.pending)
            for field in self.decimals:
                row[field] = f"{row[field]:.1f}"
            self.writer.writerow(row)
            self.file.flush()
            self.count += 1
            self.pending = None

    def close(self, status):
        try:
            self.flush_sample()
        finally:
            self.raw.close()
            self.file.close()
            self.meta.update(status=status, ended_at=utc(), packets=self.packets,
                             samples=self.count, invalid_packets=self.invalid)
            self.write_meta()


async def collect(output, reference, interval, note, console, stop=None, scanner_factory=BleakScanner, dut=None):
    stop = stop or asyncio.Event()
    recorder = Recorder(output, reference, interval, note)
    recorders = [recorder]
    try:
        if dut:
            recorders.append(Recorder(output, dut, interval, note, role="dut", shared_directory=True))
            recorder.meta["dut"] = dut
            recorder.write_meta()
    except Exception:
        recorder.close("failed")
        raise
    callback_errors = []

    def received(device, adv):
        try:
            for channel in recorders:
                channel.receive(device, adv)
        except Exception as exc:
            callback_errors.append(exc)
            stop.set()

    scanner = scanner_factory(detection_callback=received)
    status = "failed"
    console.print(f"{'REF + DUT' if dut else 'REF'} 采集中 · 间隔 {interval:g}s · Ctrl+C 保存并停止", markup=False)
    try:
        with stop_on_interrupt(stop):
            try:
                await asyncio.wait_for(scanner.start(), 15)
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), interval)
                    except TimeoutError:
                        pass
                    for channel in recorders:
                        channel.flush_sample()
                        health = ("等待有效广播" if channel.last_valid is None
                                  else "广播中断" if time.monotonic() - channel.last_valid > 10 else "正常")
                        reading = channel.last_reading
                        values = "—" if reading is None else f"{reading.temperature:.1f} °C / {reading.humidity:.1f} %RH"
                        details = ""
                        if reading is not None and channel.role == "dut":
                            details = (f" · Wi-Fi {reading.c5_soc_temp:.1f}°C"
                                       f" · SoC {reading.temperature_soc:.1f}°C"
                                       f" · CO₂ {reading.temperature_co2:.1f}°C"
                                       f" · 运行 {reading.time_uptime}s"
                                       f" · 亮 {reading.screen_brightness}"
                                       f" · 亮屏 {reading.screen_on_duration_s}s"
                                       f" · 充 {reading.power_charge_ma:+d}mA"
                                       f" · 负载 P4 {reading.cpu_load_10s}%/C5 {reading.wifi_load_10s}%")
                        console.print(f"{channel.role.upper()} · 样本 {channel.count} · 报文 {channel.packets} · 无效 {channel.invalid} · {health} · {values}{details}", markup=False)
                if callback_errors:
                    raise callback_errors[0]
                status = "stopped"
            finally:
                await scanner.stop()
    finally:
        try:
            recorder.close(status)
        finally:
            if len(recorders) > 1:
                recorders[1].close(status)
        console.print(f"采集数据已保存：{output.resolve()}", markup=False)
