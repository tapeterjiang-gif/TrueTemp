"""Versioned artifacts, written without replacing existing results."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def directory(output):
    return _directory(output, overwrite=False)


def overwritable_directory(output):
    """数据集构建用：目标目录已存在时直接整体覆盖重建。"""
    return _directory(output, overwrite=True)


@contextmanager
def _directory(output, overwrite):
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if overwrite and output.is_dir():
        # Overwrite is used by dataset build; recreate empty so stale files cannot survive.
        shutil.rmtree(output)
    # Reserve the destination so concurrent runs cannot overwrite one another.
    output.mkdir()
    temporary = Path(tempfile.mkdtemp(prefix='.truetemp-', dir=output.parent))
    try:
        yield temporary
        for item in temporary.iterdir():
            os.rename(item, output / item.name)
    except BaseException:
        shutil.rmtree(output)
        raise
    finally:
        shutil.rmtree(temporary)


def write_csv_new(frame, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as f:
        temp = Path(f.name)
    try:
        frame.to_csv(temp, index=False, encoding='utf-8-sig')
        os.link(temp, output)
    finally:
        temp.unlink(missing_ok=True)
