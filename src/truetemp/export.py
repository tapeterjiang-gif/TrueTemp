"""Export the sampled reference data from a local acquisition session."""
from datetime import datetime
import csv
import math
import os
from pathlib import Path
import tempfile


def export_csv(session: Path, output: Path | None = None, *, device="reference") -> tuple[Path, int]:
    session = session.resolve()
    if device not in ("reference", "dut"):
        raise ValueError("不支持的导出设备")
    source = session / f"{device}.csv"
    output = output or session.with_name(session.name + ("_dut" if device == "dut" else "") + ".csv")
    if output.suffix.lower() != ".csv":
        raise ValueError("导出文件必须使用 .csv 扩展名")
    if output.exists():
        raise FileExistsError(f"输出已存在，不覆盖：{output}")
    # Read and validate before publishing, so malformed input leaves no export.
    with source.open(encoding="utf-8-sig", newline="") as incoming:
        reader = csv.reader(incoming, strict=True)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"采集文件缺少表头：{source}") from None
        decimals = ("raw_temperature", "raw_humidity", "temperature_soc", "temperature_co2") if device == "dut" else ("temperature", "humidity")
        required = {"received_at", "device_id", *decimals}
        if not required.issubset(header) or len(header) != len(set(header)):
            raise ValueError(f"采集 CSV 表头不正确：{source}")
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=output.parent, prefix=".truetemp-export-")
        count = 0
        try:
            with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as outgoing:
                writer = csv.writer(outgoing)
                columns = [i for i, name in enumerate(header) if name != "monotonic_seconds"]
                writer.writerow([header[i] for i in columns])
                for row in reader:
                    if len(row) != len(header):
                        raise ValueError(f"采集 CSV 第 {reader.line_num} 行字段数量不正确")
                    time_index = header.index("received_at")
                    row[time_index] = datetime.fromisoformat(row[time_index]).isoformat(timespec="seconds")
                    for field in decimals:
                        index = header.index(field)
                        value = float(row[index])
                        if not math.isfinite(value):
                            raise ValueError(f"采集 CSV 第 {reader.line_num} 行 {field} 不是有效数值")
                        row[index] = f"{value:.1f}"
                    writer.writerow([row[i] for i in columns])
                    count += 1
            # Publish without overwriting a file created concurrently.
            os.link(temporary, output)
        finally:
            os.unlink(temporary)
    return output.resolve(), count
