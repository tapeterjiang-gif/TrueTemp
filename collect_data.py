"""
TrueTemp 数据采集命令
=====================

BLE 实时采集 REF（SHT4x 参考表）与 DUT（Qingping 设备）广播，采集中实时输出
状态行（与 Codex truetemp collect 同格式），Ctrl+C 停止后保存为 xlsx。

用法:
    python3 collect_data.py scan                     # 扫描 BLE 设备
    python3 collect_data.py collect --ref UUID --dut UUID [--output data] [--interval 5]
    python3 collect_data.py collect --demo 5         # 模拟采集 5 秒（无硬件验证）

输出:
    data/YYYYMMDD_HHMM.xlsx
      REF sheet:  timestamp, temperature, humidity
      DUT sheet:  received_at, raw_temperature, raw_humidity, temperature_soc,
                  temperature_co2, time_uptime, screen_brightness,
                  screen_on_duration_s, cpu_load_10s, wifi_load_10s,
                  power_charge_ma, c5_soc_temp
依赖:
    bleak（BLE 扫描）; xlsx 写入使用 Python 标准库
"""

import argparse
import asyncio
import importlib.util
import signal
import struct
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))


def local_now():
    return datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# ---------------------------------------------------------------- 协议解码
REF_COMPANY_ID = 0x06D5
DUT_COMPANY_ID = 0xDFDF


@dataclass
class Reading:
    gadget_id: int
    temperature_raw: int
    humidity_raw: int
    temperature: float
    humidity: float


def decode_ref(data: bytes) -> Reading:
    """Sensirion live advertisement type 0x00, sample type 0x06."""
    if len(data) != 8:
        raise ValueError(f'SHT4x 载荷应为 8 字节，实际为 {len(data)} 字节')
    if data[:2] != b'\x00\x06':
        raise ValueError(f'不支持的 Sensirion 广播类型：{data[:2].hex()}')
    gadget_id, raw_t, raw_h = struct.unpack_from('<HHH', data, 2)
    temperature = -45 + 175 * raw_t / 65535
    humidity = -6 + 125 * raw_h / 65535
    if not 0 <= humidity <= 100:
        raise ValueError(f'湿度超出物理范围：{humidity:.3f} %RH')
    return Reading(gadget_id, raw_t, raw_h, temperature, humidity)


@dataclass
class DutReading:
    version: int
    flags: int
    raw_temperature: float
    raw_humidity: float
    temperature_soc: float
    temperature_co2: float
    time_uptime: int
    screen_brightness: int
    screen_on_duration_s: int
    cpu_load_10s: int
    wifi_load_10s: int
    power_charge_ma: int
    c5_soc_temp: float

    @property
    def temperature(self):
        return self.raw_temperature

    @property
    def humidity(self):
        return self.raw_humidity


def decode_dut(data: bytes) -> DutReading:
    """DUT 遥测协议 v3，21 字节（Bleak 已剥离 2 字节公司 ID），全部小端。"""
    if len(data) != 21:
        raise ValueError(f'DUT v3 载荷应为 21 字节，实际为 {len(data)} 字节')
    (version, flags, raw_temperature, raw_humidity, temperature_soc, temperature_co2,
     time_uptime, screen_brightness, screen_on_duration_s, cpu_load_10s, wifi_load_10s,
     power_charge_ma, c5_soc_temp) = struct.unpack('<BBhhhhHBHBBhh', data)
    if version != 3:
        raise ValueError(f'不支持的 DUT 协议版本：{version}')
    reading = DutReading(version, flags, raw_temperature / 10, raw_humidity / 10,
                         temperature_soc / 10, temperature_co2 / 10, time_uptime,
                         screen_brightness, screen_on_duration_s, cpu_load_10s,
                         wifi_load_10s, power_charge_ma, c5_soc_temp / 10)
    if not 0 <= reading.raw_humidity <= 100:
        raise ValueError('DUT 相对湿度超出 0～100%RH')
    if reading.time_uptime > 7200 or reading.screen_on_duration_s > 3600:
        raise ValueError('DUT 计时字段超过协议上限')
    if reading.cpu_load_10s > 100 or reading.wifi_load_10s > 100:
        raise ValueError('DUT 负载超过 100%')
    return reading


# ---------------------------------------------------------------- xlsx 写入
def _col_letter(i):
    s = ''
    while i >= 0:
        s = chr(65 + i % 26) + s
        i = i // 26 - 1
    return s


def _sheet_xml(header, rows):
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']

    def cell(r, c, v):
        ref = _col_letter(c) + str(r)
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v!r}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'

    parts.append('<row r="1">' + ''.join(cell(1, i, h) for i, h in enumerate(header)) + '</row>')
    for rn, row in enumerate(rows, 2):
        parts.append('<row r="%d">' % rn + ''.join(cell(rn, i, row[i]) for i in range(len(header))) + '</row>')
    parts.append('</sheetData></worksheet>')
    return ''.join(parts)


def write_xlsx(path, ref_rows, dut_rows):
    """ref_rows: [[ts, temp, hum], ...]; dut_rows: [[ts, 11 字段], ...]"""
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="REF" sheetId="1" r:id="rId1"/>'
          '<sheet name="DUT" sheetId="2" r:id="rId2"/></sheets></workbook>')
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
               '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
               '</Relationships>')

    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ct)
        z.writestr('_rels/.rels', rels)
        z.writestr('xl/workbook.xml', wb)
        z.writestr('xl/_rels/workbook.xml.rels', wb_rels)
        z.writestr('xl/worksheets/sheet1.xml', _sheet_xml(['timestamp', 'temperature', 'humidity'], ref_rows))
        z.writestr('xl/worksheets/sheet2.xml',
                   _sheet_xml(['received_at', 'raw_temperature', 'raw_humidity', 'temperature_soc',
                               'temperature_co2', 'time_uptime', 'screen_brightness',
                               'screen_on_duration_s', 'cpu_load_10s', 'wifi_load_10s',
                               'power_charge_ma', 'c5_soc_temp'], dut_rows))


# ---------------------------------------------------------------- 采集器
DUT_DECIMAL_FIELDS = ('raw_temperature', 'raw_humidity', 'temperature_soc',
                      'temperature_co2', 'c5_soc_temp')


class Recorder:
    """接收并解码广播，按采样间隔输出状态行，样本缓存在 pending。"""

    def __init__(self, role, device_id, interval):
        self.role = role
        self.device_id = (device_id or '').casefold()
        self.interval = interval
        self.company = DUT_COMPANY_ID if role == 'dut' else REF_COMPANY_ID
        self.decoder = decode_dut if role == 'dut' else decode_ref
        self.samples = []          # xlsx 数据行
        self.count = 0             # 已落盘样本数
        self.packets = 0           # 收到报文总数
        self.invalid = 0           # 无效报文数
        self.pending = None
        self.last_valid = None
        self.last_reading = None

    def receive(self, device_address, adv):
        if self.device_id and device_address.casefold() != self.device_id:
            return
        payload = adv.manufacturer_data.get(self.company)
        if payload is None:
            return
        self.packets += 1
        try:
            reading = self.decoder(payload)
        except ValueError:
            self.invalid += 1
            return
        self.pending = reading
        self.last_valid = time.monotonic()
        self.last_reading = reading

    def flush_sample(self):
        if self.pending is None:
            return
        r = self.pending
        if self.role == 'dut':
            row = [local_now(), round(r.raw_temperature, 1), round(r.raw_humidity, 1),
                   round(r.temperature_soc, 1), round(r.temperature_co2, 1),
                   r.time_uptime, r.screen_brightness, r.screen_on_duration_s,
                   r.cpu_load_10s, r.wifi_load_10s, r.power_charge_ma, round(r.c5_soc_temp, 1)]
        else:
            row = [local_now(), round(r.temperature, 1), round(r.humidity, 1)]
        self.samples.append(row)
        self.count += 1
        self.pending = None

    def status_line(self):
        health = ('等待有效广播' if self.last_valid is None
                  else '广播中断' if time.monotonic() - self.last_valid > 10 else '正常')
        r = self.last_reading
        values = '—' if r is None else f'{r.temperature:.1f} °C / {r.humidity:.1f} %RH'
        details = ''
        if r is not None and self.role == 'dut':
            details = (f' · Wi-Fi {r.c5_soc_temp:.1f}°C'
                       f' · SoC {r.temperature_soc:.1f}°C'
                       f' · CO₂ {r.temperature_co2:.1f}°C'
                       f' · 运行 {r.time_uptime}s'
                       f' · 亮 {r.screen_brightness}'
                       f' · 亮屏 {r.screen_on_duration_s}s'
                       f' · 充 {r.power_charge_ma:+d}mA'
                       f' · 负载 P4 {r.cpu_load_10s}%/C5 {r.wifi_load_10s}%')
        role = 'REFERENCE' if self.role == 'reference' else 'DUT'
        return (f'{role} · 样本 {self.count} · 报文 {self.packets}'
                f' · 无效 {self.invalid} · {health} · {values}{details}')


def status_line(recorders):
    return '\n'.join(r.status_line() for r in recorders)


# ---------------------------------------------------------------- 采集主流程
def _demo_reading(role, t):
    """生成模拟广播载荷，用于 --demo 验证（无硬件）。"""
    if role == 'dut':
        payload = struct.pack('<BBhhhhHBHBBhh', 3, 0,
                              int((32.5 + t * 0.1) * 10), int((24.0 + t * 0.2) * 10),
                              int((47.0 + t * 0.5) * 10), int((41.0 + t * 0.4) * 10),
                              7200 - int(t), 80, 0, 56, 1, 0, int(44.0 * 10))
    else:
        # type(2B) + gadget_id(2B) + raw_t(2B) + raw_h(2B)
        payload = struct.pack('<HHHH', 0x0600, 1630, 0x69E2, 0x5758)
    return payload


async def run_collect(ref, dut, output, interval, demo=False):
    if demo:
        from collections import namedtuple
        Adv = namedtuple('Adv', ['manufacturer_data', 'rssi'])
        ref = ref or 'DEMO-REF'
        dut = dut or 'DEMO-DUT'
    else:
        from bleak import BleakScanner

    recorders = [Recorder('reference', ref, interval)]
    if dut:
        recorders.append(Recorder('dut', dut, interval))

    # 输出文件：以启动采集时间命名，每满 50 条落盘一次
    started_at = datetime.now(CST)
    out_dir = Path(output)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{started_at.strftime('%Y%m%d_%H%M')}.xlsx"
    ref_rec = next((r for r in recorders if r.role == 'reference'), None)
    dut_rec = next((r for r in recorders if r.role == 'dut'), None)
    last_saved = {'ref': 0, 'dut': 0}

    def save_once():
        if ref_rec is None or ref_rec.count == 0:
            return False
        write_xlsx(path, ref_rec.samples, dut_rec.samples if dut_rec else [])
        return True

    def save_if_due(force=False):
        if ref_rec is None:
            return
        # REF 或 DUT 任一通道每满 50 条即落盘（以快的为准）
        due = (force
               or (ref_rec.count and ref_rec.count % 50 == 0 and ref_rec.count != last_saved['ref'])
               or (dut_rec and dut_rec.count and dut_rec.count % 50 == 0 and dut_rec.count != last_saved['dut']))
        if due:
            if save_once():
                last_saved['ref'] = ref_rec.count
                if dut_rec:
                    last_saved['dut'] = dut_rec.count
                print(f'  已写入 REF {ref_rec.count} 条 / DUT {dut_rec.count if dut_rec else 0} 条 → {path.name}',
                      flush=True)

    stop = asyncio.Event()

    def received(device, adv):
        for ch in recorders:
            ch.receive(device.address, adv)

    print(f"{'REF + DUT' if dut else 'REF'} 采集中 · 间隔 {interval:g}s · Ctrl+C 保存并停止", flush=True)

    if demo:
        # 模拟回调：每 0.5 秒注入一条 REF + DUT 广播
        t0 = time.monotonic()

        async def tick():
            while not stop.is_set():
                await asyncio.sleep(0.5)
                t = time.monotonic() - t0
                for ch in recorders:
                    adv = Adv({ch.company: _demo_reading(ch.role, t)}, -50)
                    ch.receive(ch.device_id or 'DEMO-DEV', adv)
                # 与真实流程一致：按间隔 flush 并输出状态行
                for ch in recorders:
                    ch.flush_sample()
                save_if_due()
                print(status_line(recorders), flush=True)
                if time.monotonic() - t0 > demo:
                    stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            await asyncio.gather(tick())
        finally:
            pass
    else:
        def handler():
            stop.set()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handler)

        scanner = BleakScanner(detection_callback=received)
        try:
            await asyncio.wait_for(scanner.start(), 15)
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), interval)
                except asyncio.TimeoutError:
                    pass
                for ch in recorders:
                    ch.flush_sample()
                save_if_due()
                print(status_line(recorders), flush=True)
        finally:
            await scanner.stop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    # 停止前最终保存
    save_if_due(force=True)
    if ref_rec is not None and ref_rec.count:
        print(f'采集数据已保存：{path.resolve()}（REF {ref_rec.count} 样本'
              f' / DUT {dut_rec.count if dut_rec else 0} 样本）', flush=True)
    else:
        print('未采集到数据，未保存。', flush=True)


# ---------------------------------------------------------------- 扫描
CONFIG_FILE = Path(__file__).resolve().parent / 'truetemp.toml'


def load_config():
    cfg = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def save_config(ref, dut):
    lines = ['# TrueTemp 设备配置（collect 默认使用，可用 --ref/--dut 覆盖）', '']
    if ref:
        lines.append(f'ref = "{ref}"')
    if dut:
        lines.append(f'dut = "{dut}"')
    CONFIG_FILE.write_text('\n'.join(lines) + '\n')
    print(f'已保存设备配置：{CONFIG_FILE.resolve()}', flush=True)


async def _scan(seconds):
    """扫描 BLE 广播，返回 {address: (name, rssi, role, mfr_desc, svc)}。"""
    from bleak import BleakScanner

    devices = {}
    stop = asyncio.Event()

    def received(device, adv):
        # 记录所有广播设备；无 mfr 的广播（如扫描响应/轮播包）不覆盖已识别信息
        mfr = adv.manufacturer_data
        prev = devices.get(device.address)
        name = adv.local_name or device.name or (prev[1] if prev else '—')
        if not mfr:
            if prev:
                devices[device.address] = (prev[0], name, adv.rssi, prev[3], prev[4],
                                           prev[5] if prev[5] != '—' else (','.join(adv.service_uuids) or '—'))
            return
        role = '?'
        desc = []
        for cid, payload in mfr.items():
            if cid == DUT_COMPANY_ID:
                role = 'DUT'
            elif cid == REF_COMPANY_ID:
                role = 'REF'
            desc.append(f'0x{cid:04X}:{payload.hex()[:20]}')
        if prev and prev[4] != '—':
            desc = list(dict.fromkeys([*prev[4].split(', '), *desc]))
        svc = ','.join(adv.service_uuids) or (prev[5] if prev else '—')
        devices[device.address] = (device.address, name, adv.rssi,
                                   role if role != '?' else (prev[3] if prev else '?'),
                                   ', '.join(desc), svc)

    def handler():
        stop.set()

    scanner = BleakScanner(detection_callback=received)
    print(f'扫描中 · {seconds:g}s · Ctrl+C 停止', flush=True)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handler)
    try:
        await asyncio.wait_for(scanner.start(), 15)
        try:
            await asyncio.wait_for(stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass
    finally:
        await scanner.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    return devices


async def scan_devices(seconds):
    devices = await _scan(seconds)
    print('\nTrueTemp · BLE 广播', flush=True)
    for addr, name, rssi, role, mfr, svc in devices.values():
        print(f'  {role:<3s} {addr}  {name:<22s} {rssi:>4d} dBm  mfr[{mfr}]  svc[{svc}]', flush=True)
    if not devices:
        print('未发现任何 BLE 广播。', flush=True)
        print('检查项：1) 系统设置 → 隐私与安全性 → 蓝牙 中允许当前终端；2) 设备广播是否开启；3) 距离过远。', flush=True)


async def set_devices(seconds):
    """扫描后交互选择 REF/DUT 并保存到 truetemp.toml。"""
    devices = await _scan(seconds)
    if not devices:
        print('未发现任何 BLE 广播，无法选择。', flush=True)
        return
    items = sorted(devices.items(), key=lambda kv: (kv[1][3] != 'REF', kv[1][3] != 'DUT', kv[1][3], kv[1][1]))
    print('\nTrueTemp · 请选择设备', flush=True)
    for i, (addr, (_, name, rssi, role, mfr, _)) in enumerate(items, 1):
        print(f'  {i:>2d}  {role:<3s} {addr}  {name:<22s} {rssi:>4d} dBm  mfr[{mfr}]', flush=True)
    print()
    cfg = load_config()
    try:
        choice = input('选择 REFERENCE 编号（回车跳过）: ').strip()
        ref_uuid = items[int(choice) - 1][0] if choice else None
        choice = input('选择 DUT 编号（回车跳过）: ').strip()
        dut_uuid = items[int(choice) - 1][0] if choice else None
    except (ValueError, IndexError):
        print('编号无效，未保存。', flush=True)
        return
    ref_uuid = ref_uuid or cfg.get('ref', '')
    dut_uuid = dut_uuid or cfg.get('dut', '')
    save_config(ref_uuid, dut_uuid)
    print(f'  REF = {ref_uuid or "（未设置）"}', flush=True)
    print(f'  DUT = {dut_uuid or "（未设置）"}', flush=True)


# ---------------------------------------------------------------- 设备诊断
async def probe_device(device_id, seconds):
    """监听指定设备广播详情并尝试连接枚举服务，用于排查广播/识别问题。"""
    from bleak import BleakClient, BleakScanner

    device_id = device_id.casefold()
    print(f'监听 {device_id} · {seconds:g}s · Ctrl+C 停止', flush=True)
    stop = asyncio.Event()

    def handler():
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handler)

    seen = {'any': False}

    def received(device, adv):
        if device.address.casefold() != device_id:
            return
        seen['any'] = True
        mfr = ', '.join(f'0x{cid:04X}:{p.hex()}' for cid, p in adv.manufacturer_data.items()) or '—'
        svc = ','.join(adv.service_uuids) or '—'
        tx = f' tx={adv.tx_power}dBm' if adv.tx_power is not None else ''
        print(f'  [{datetime.now(CST).strftime("%H:%M:%S")}] rssi={adv.rssi:>4d}dBm{tx}'
              f' name={adv.local_name or "—"!r} mfr[{mfr}] svc[{svc}]', flush=True)

    scanner = BleakScanner(detection_callback=received)
    try:
        await asyncio.wait_for(scanner.start(), 15)
        try:
            await asyncio.wait_for(stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass
    finally:
        await scanner.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)

    if not seen['any']:
        print('监听期间未收到该地址的任何广播。', flush=True)
        return

    print('\n尝试连接并枚举服务…', flush=True)
    try:
        async with BleakClient(device_id, timeout=10) as client:
            print(f'已连接。MTU={client.mtu_size}', flush=True)
            for service in client.services:
                name = f' ({service.description})' if service.description else ''
                print(f'  SERVICE {service.uuid}{name}', flush=True)
                for char in service.characteristics:
                    print(f'    CHAR  {char.uuid}  [{",".join(char.properties)}]', flush=True)
    except Exception as exc:
        print(f'连接失败：{exc}', flush=True)


# ---------------------------------------------------------------- 入口
def main():
    parser = argparse.ArgumentParser(description='TrueTemp 数据采集（REF/DUT 广播 → xlsx）')
    sub = parser.add_subparsers(dest='command', required=True)

    p_scan = sub.add_parser('scan', help='扫描 BLE 广播中的 REF/DUT 设备')
    p_scan.add_argument('--seconds', type=float, default=30)

    p_set = sub.add_parser('set', help='扫描后交互选择 REF/DUT 并保存到 truetemp.toml')
    p_set.add_argument('--seconds', type=float, default=30)

    p_probe = sub.add_parser('probe', help='监听指定设备广播详情并连接枚举服务（诊断）')
    p_probe.add_argument('device', help='设备 BLE UUID（scan 获取）')
    p_probe.add_argument('--seconds', type=float, default=30)

    p_collect = sub.add_parser('collect', help='采集 REF/DUT 广播并保存为 xlsx')
    p_collect.add_argument('--ref', help='REFERENCE 设备 BLE UUID（可先用 scan 获取）')
    p_collect.add_argument('--dut', help='DUT 设备 BLE UUID（可选）')
    p_collect.add_argument('--output', default='data', help='输出目录（默认 data/）')
    p_collect.add_argument('--interval', type=float, default=5.0, help='采样/刷新间隔秒数（默认 5）')
    p_collect.add_argument('--demo', type=float, default=0, help='模拟采集 N 秒（无硬件验证）')

    args = parser.parse_args()

    # 真实扫描/采集需要 bleak；--demo 模拟无需蓝牙
    if args.command in ('scan', 'set') or (args.command == 'collect' and not args.demo):
        if importlib.util.find_spec('bleak') is None:
            print('缺少依赖 bleak。请用项目虚拟环境运行：', file=sys.stderr)
            print('  .venv/bin/python collect_data.py ...', file=sys.stderr)
            print('或先安装：.venv/bin/pip install bleak', file=sys.stderr)
            sys.exit(1)

    if args.command == 'scan':
        asyncio.run(scan_devices(args.seconds))
    elif args.command == 'set':
        asyncio.run(set_devices(args.seconds))
    elif args.command == 'probe':
        asyncio.run(probe_device(args.device, args.seconds))
    else:
        if args.demo:
            asyncio.run(run_collect(args.ref, args.dut, args.output, args.interval, demo=True))
        else:
            cfg = load_config()
            ref = args.ref or cfg.get('ref')
            dut = args.dut or cfg.get('dut')
            if not ref:
                parser.error('collect 需要 --ref UUID（或先运行 ./collect set 保存配置）')
            asyncio.run(run_collect(ref, dut, args.output, args.interval))


if __name__ == '__main__':
    main()
