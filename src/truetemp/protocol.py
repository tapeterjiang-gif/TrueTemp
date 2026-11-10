"""Sensirion live advertisement type 0x00, sample type 0x06.

Source: https://github.com/Sensirion/ble-services/blob/main/sensirion-ble/src/resources/ble-ad-sample-types.yml
Bleak removes the two-byte company ID from manufacturer_data values.
"""
from dataclasses import dataclass
import struct
from typing import Optional

COMPANY_ID = 0x06D5


@dataclass(frozen=True)
class Reading:
    gadget_id: int
    temperature_raw: int
    humidity_raw: int
    temperature: float
    humidity: float


def decode(data: bytes) -> Reading:
    if len(data) != 8:
        raise ValueError(f"SHT4x 载荷应为 8 字节，实际为 {len(data)} 字节")
    if data[:2] != b"\x00\x06":
        raise ValueError(f"不支持的 Sensirion 广播类型：{data[:2].hex()}")
    gadget_id, raw_t, raw_h = struct.unpack_from("<HHH", data, 2)
    temperature = -45 + 175 * raw_t / 65535
    humidity = -6 + 125 * raw_h / 65535
    # Preserve out-of-range engineering values in raw logs, never silently clip.
    if not 0 <= humidity <= 100:
        raise ValueError(f"湿度超出物理范围：{humidity:.3f} %RH")
    return Reading(gadget_id, raw_t, raw_h, temperature, humidity)

DUT_COMPANY_ID = 0xDFDF
DUT_DECIMAL_FIELDS = ("raw_temperature", "raw_humidity", "temperature_soc", "temperature_co2", "c5_soc_temp")


@dataclass(frozen=True)
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
    cpu_load_10s: float
    wifi_load_10s: Optional[int]
    power_charge_ma: int
    c5_soc_temp: Optional[float]
    wifi_tx_bytes: Optional[int] = None
    ambient_lux: Optional[int] = None
    power_charge_mv: Optional[int] = None

    @property
    def temperature(self):
        return self.raw_temperature

    @property
    def humidity(self):
        return self.raw_humidity

    def status_details(self):
        details = (f" · SoC {self.temperature_soc:.1f}°C · CO₂ {self.temperature_co2:.1f}°C"
                   f" · 运行 {self.time_uptime}s · 亮 {self.screen_brightness}"
                   f" · 亮屏 {self.screen_on_duration_s}s · 充 {self.power_charge_ma:+d}mA"
                   f" · CPU {self.cpu_load_10s:g}%")
        if self.version == 3:
            return details + f" · Wi-Fi {self.c5_soc_temp:.1f}°C/{self.wifi_load_10s}%"
        light = "—" if self.ambient_lux is None else f"{self.ambient_lux} lux"
        return details + f" · Wi-Fi TX {self.wifi_tx_bytes} B/s · 光感 {light}"


def decode_dut(data: bytes) -> DutReading:
    """Decode Lite2 v3 or Snow2S v4 after Bleak strips the company ID."""
    if data[:1] == b"\x04":
        return decode_snow2s(data)
    if len(data) != 21:
        raise ValueError(f"DUT v3 载荷应为 21 字节，实际为 {len(data)} 字节")
    (version, flags, raw_temperature, raw_humidity, temperature_soc, temperature_co2,
     time_uptime, screen_brightness, screen_on_duration_s, cpu_load_10s, wifi_load_10s,
     power_charge_ma, c5_soc_temp) = struct.unpack("<BBhhhhHBHBBhh", data)
    if version != 3:
        raise ValueError(f"不支持的 DUT 协议版本：{version}")
    reading = DutReading(version, flags, raw_temperature / 10, raw_humidity / 10, temperature_soc / 10,
                         temperature_co2 / 10, time_uptime, screen_brightness, screen_on_duration_s,
                         cpu_load_10s, wifi_load_10s, power_charge_ma, c5_soc_temp / 10)
    if not 0 <= reading.raw_humidity <= 100:
        raise ValueError("DUT 相对湿度超出 0～100%RH")
    if reading.time_uptime > 7200 or reading.screen_on_duration_s > 3600:
        raise ValueError("DUT 计时字段超过协议上限")
    if reading.cpu_load_10s > 100 or reading.wifi_load_10s > 100:
        raise ValueError("DUT 负载超过 100%")
    # Zero is preserved: v3 has no validity bits to distinguish unavailable data.
    return reading


def decode_snow2s(data: bytes) -> DutReading:
    if len(data) != 35 or data[0] != 4:
        raise ValueError("Snow2S v4 载荷应为 35 字节")
    (version, flags, _id, raw_t, raw_rh, soc_t, co2_t, uptime, brightness, screen_on_s,
     cpu, lux, _rssi, tx, charge_ma, charge_mv, _mac) = struct.unpack("<BBHhhhhHHHHHbHHH6s", data)
    if not 0 <= raw_rh <= 1000 or brightness > 1000 or cpu > 1000 or max(uptime, screen_on_s) > 7200:
        raise ValueError("Snow2S v4 湿度、背光、CPU 或计时字段超出协议范围")
    return DutReading(version, flags, raw_t / 10, raw_rh / 10, soc_t / 10, co2_t / 10,
                      uptime, brightness, screen_on_s, cpu / 10, None, charge_ma, None,
                      wifi_tx_bytes=tx, ambient_lux=lux if flags & 0x80 else None, power_charge_mv=charge_mv)
