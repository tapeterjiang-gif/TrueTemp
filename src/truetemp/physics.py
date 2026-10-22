"""Sonntag (1994), saturation over liquid water, pressure in hPa."""
import numpy as np

PHYSICS = "sonntag1994-liquid-hpa-v1"


def saturation_pressure(temperature):
    t = np.asarray(temperature, dtype=float)
    if not np.all(np.isfinite(t)) or np.any((t < -40) | (t > 125)):
        raise ValueError("物理计算温度必须在 -40～125°C 且为有限值")
    k = t + 273.15
    return np.exp(-6096.9385 / k + 16.635794 - .02711193 * k
                  + .00001673952 * k**2 + 2.433502 * np.log(k))


def vapor_pressure(temperature, humidity):
    return np.asarray(humidity, dtype=float) / 100 * saturation_pressure(temperature)


def relative_humidity(temperature, pressure):
    return 100 * np.asarray(pressure) / saturation_pressure(temperature)


def dew_point(pressure):
    """露点温度 (°C)：反解 Sonntag 1994 液态水饱和式 es(T_dew) = e（hPa）。"""
    e = np.asarray(pressure, dtype=float)
    if e.size == 0:
        return e
    if not np.all(np.isfinite(e)) or (e < 0).any():
        raise ValueError('水汽压必须为有限的非负值（hPa）')
    valid = e > 1e-9
    t = np.full_like(e, 60.0)
    for _ in range(40):
        k = t + 273.15
        # log es(t)（对数避免溢出）及其对 t 的导数
        log_es = -6096.9385 / k + 16.635794 - .02711193 * k + .00001673952 * k**2 + 2.433502 * np.log(k)
        dlog = 6096.9385 / k**2 - .02711193 + 2 * .00001673952 * k + 2.433502 / k
        log_e = np.log(np.maximum(e, 1e-9))
        t = t - (log_es - np.where(valid, log_e, 0.0)) / dlog
    return np.where(valid, t, np.nan)


def absolute_humidity(pressure, temperature):
    """绝对湿度 (g/m³)：由水汽压 e（hPa）与温度 T（°C）推导，216.7 = 217 常数。"""
    return 216.7 * np.asarray(pressure, dtype=float) / (np.asarray(temperature, dtype=float) + 273.15)
