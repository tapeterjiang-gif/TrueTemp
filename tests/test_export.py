import csv

import pytest

from truetemp.cli import main
from truetemp.export import export_csv


def session_at(tmp_path, rows=None):
    session = tmp_path / "ref01"
    session.mkdir()
    with (session / "reference.csv").open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["received_at", "device_id", "temperature", "humidity"])
        writer.writerows(rows if rows is not None else [["2026-09-06T12:00:00.987654+00:00", "REF-中文", "-10.125", "44.0"]])
    return session


def test_export_preserves_values_and_defaults(tmp_path):
    session = session_at(tmp_path)
    output, count = export_csv(session)
    assert output == tmp_path / "ref01.csv"
    assert count == 1
    with output.open(encoding="utf-8-sig", newline="") as file:
        row = list(csv.DictReader(file))[0]
    assert row["temperature"] == "-10.1"
    assert row["humidity"] == "44.0"
    assert row["device_id"] == "REF-中文"
    assert row["received_at"] == "2026-09-06T12:00:00+00:00"


def test_export_command_needs_no_config(tmp_path, monkeypatch):
    session = session_at(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "truetemp.toml").write_text("invalid = [")
    output = tmp_path / "exports" / "readings.csv"
    assert main(["export", str(session), "--output", str(output)]) == 0
    assert output.is_file()
    original = output.read_bytes()
    assert main(["export", str(session), "--output", str(output)]) == 1
    assert output.read_bytes() == original


def test_invalid_row_leaves_no_output(tmp_path):
    session = session_at(tmp_path, [["truncated"]])
    with pytest.raises(ValueError):
        export_csv(session)
    assert not (tmp_path / "ref01.csv").exists()
    assert not list(tmp_path.glob(".truetemp-export-*"))


def test_empty_session_exports_header(tmp_path):
    session = session_at(tmp_path, [])
    output, count = export_csv(session)
    assert count == 0 and output.is_file()


def test_missing_session(tmp_path):
    assert main(["export", str(tmp_path / "missing")]) == 1
    assert not (tmp_path / "missing.csv").exists()


def test_legacy_monotonic_column_is_omitted(tmp_path):
    session = session_at(tmp_path)
    source = session / "reference.csv"
    source.write_text("received_at,monotonic_seconds,device_id,temperature,humidity\n2026-09-06T12:00:00+00:00,1234.5,REF,-10,44\n")
    original = source.read_bytes()
    output, count = export_csv(session)
    with output.open(encoding="utf-8-sig", newline="") as file:
        row = list(csv.DictReader(file))[0]
    assert count == 1
    assert row == dict(received_at="2026-09-06T12:00:00+00:00", device_id="REF", temperature="-10.0", humidity="44.0")
    assert source.read_bytes() == original
