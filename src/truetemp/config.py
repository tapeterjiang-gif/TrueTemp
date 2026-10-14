from pathlib import Path
import os
import re
import tempfile
import tomlkit


def duration(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
    if not match:
        raise ValueError("时间须包含单位，例如 2s 或 500ms")
    number, unit = match.groups()
    seconds = float(number) * {"ms": .001, "s": 1, "m": 60, "h": 3600}[unit]
    if not 0 < seconds <= 86400:
        raise ValueError("时间必须大于 0 且不超过 24 小时")
    return seconds


def read_config(path: Path):
    config = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    for key in ("devices", "collection"):
        if key in config and not isinstance(config[key], dict):
            raise ValueError(f"配置 {key} 必须为表")
    validate_devices(config.get("devices", {}))
    return config


def validate_devices(devices):
    for role in ("reference", "dut"):
        value = devices.get(role)
        if value is not None and (not isinstance(value, str) or not value.strip()
                                  or any(ord(c) < 32 for c in value)):
            raise ValueError(f"devices.{role} 必须为非空设备标识")
    ref, dut = devices.get("reference"), devices.get("dut")
    if ref and dut and ref.strip().casefold() == dut.strip().casefold():
        raise ValueError("REF 和 DUT 不能使用同一个设备标识")


def save_reference(path: Path, reference: str):
    save_devices(path, {"reference": reference})


def save_devices(path: Path, changes: dict):
    if any(role not in ("reference", "dut") for role in changes):
        raise ValueError("不支持的设备角色")
    config = read_config(path)
    if "devices" not in config:
        config["devices"] = tomlkit.table()
    for role, value in changes.items():
        config["devices"][role] = value.strip()
    validate_devices(config["devices"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".truetemp-", suffix=".toml")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(tomlkit.dumps(config))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
