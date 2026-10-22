"""Reconstruct paired samples from original BLE payloads; explicit experiment splits."""
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import hashlib
import json
import math
import tomllib
import numpy as np
import pandas as pd
from .artifacts import digest, overwritable_directory, read_json, write_json
from .config import duration
from .protocol import decode, decode_dut
from .features import UNITS

SPLITS = ('train', 'validation', 'test')


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('时间必须带时区')
    return parsed.timestamp()


def assert_independent(left, right):
    for a in left:
        for b in right:
            if a['experiment_id'] == b['experiment_id']:
                raise ValueError('实验组跨集合重复：' + a['experiment_id'])
            if set(a['hashes'].values()) & set(b['hashes'].values()):
                raise ValueError('原始日志跨集合重复')
            for role in ('reference', 'dut'):
                if a[role].casefold() == b[role].casefold():
                    if max(a['utc_start'], b['utc_start']) <= min(a['utc_end'], b['utc_end']):
                        raise ValueError('同一设备的采集时间跨集合重叠')


def raw_fingerprint(path):
    canonical = hashlib.sha256()
    for line in path.read_text().splitlines():
        canonical.update(json.dumps(json.loads(line), sort_keys=True, separators=(',', ':'), allow_nan=False).encode())
        canonical.update(b'\n')
    return canonical.hexdigest()


def read_events(path, device, role):
    events, invalid = [], 0
    previous_mono = float('-inf')
    for line, text in enumerate(path.read_text().splitlines(), 1):
        try:
            row = json.loads(text)
            if row['device_id'].casefold() != device.casefold():
                raise ValueError('原始日志设备身份不一致')
            mono = float(row['monotonic_seconds'])
            if not math.isfinite(mono):
                raise ValueError('无效单调时间')
            when = timestamp(row['received_at'])
            if mono < previous_mono:
                raise ValueError(f'{path}:{line} 单调时钟倒退，无法可靠重建会话')
            previous_mono = mono
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f'{path}:{line} 原始记录无效') from exc
        if row.get('company_id') != (0x06D5 if role == 'reference' else 0xDFDF):
            raise ValueError(f'{path}:{line} 厂商标识不匹配')
        try:
            value = (decode if role == 'reference' else decode_dut)(bytes.fromhex(row['payload_hex']))
        except (ValueError, KeyError):
            invalid += 1
            continue
        events.append(dict(asdict(value), mono=mono, utc=when, received_at=row['received_at'], line=line))
    events.sort(key=lambda x: (x['mono'], x['line']))
    return events, invalid


def format_ended_at(meta: dict, fallback_epoch: float) -> str:
    """会话结束时间转本地并格式化为到分钟；优先取 session 元数据，缺失时退回目录修改时间。"""
    raw = meta.get('ended_at')
    if raw:
        try:
            return datetime.fromisoformat(raw).astimezone().strftime('%Y-%m-%d %H:%M')
        except (TypeError, ValueError):
            pass
    return datetime.fromtimestamp(fallback_epoch).astimezone().strftime('%Y-%m-%d %H:%M')


def recent_sessions(data_dir="data", limit=5) -> list[tuple[Path, dict]]:
    """返回最近已正常关闭的采集会话（session.json+dut_session.json 均存在且非 running），
    按目录修改时间倒序，最多 limit 个。meta 含 ending 显示所需字段。"""
    root = Path(data_dir)
    found = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        if not (path / 'session.json').is_file() or not (path / 'dut_session.json').is_file():
            continue
        refmeta, dutmeta = read_json(path / 'session.json'), read_json(path / 'dut_session.json')
        if any(m.get('status') == 'running' for m in (refmeta, dutmeta)):
            continue
        mtime = path.stat().st_mtime
        meta = dict(interval_seconds=refmeta.get('interval_seconds'),
                    ended_at=format_ended_at(refmeta, mtime),
                    ref_samples=refmeta.get('samples', 0), dut_samples=dutmeta.get('samples', 0))
        found.append((mtime, path.resolve(), meta))
    found.sort(key=lambda item: item[0], reverse=True)
    return [(p, m) for _, p, m in found[:limit]]


def domain_exclusions(frame, gap):
    """领域化离群审计：返回每条规则命中行数与说明（仅诊断，不剔除样本）。"""
    rules = [
        ('raw_temperature 超出目标 0～60°C', frame.raw_temperature.lt(0) | frame.raw_temperature.gt(60)),
        ('ref_temperature 超出目标 0～60°C', frame.ref_temperature.lt(0) | frame.ref_temperature.gt(60)),
        ('原始湿度为 0%（无效）', frame.raw_humidity.eq(0)),
        ('配对时差 ≥2× 容差', frame.pair_gap_seconds.ge(2 * gap)),
    ]
    return [dict(reason=name, count=int(mask.sum())) for name, mask in rules if mask.any()]


def build_dataset(manifest, output):
    manifest = Path(manifest).resolve()
    spec = tomllib.loads(manifest.read_text())
    sessions = spec.get('sessions', [])
    if not sessions:
        raise ValueError('清单需要 [[sessions]]')
    gap = duration(str(spec.get('alignment', {}).get('max_gap', '2s')))
    sources, frames, quality = [], [], {s: [] for s in SPLITS}
    for entry in sessions:
        if not all(isinstance(entry.get(k), str) and entry[k] for k in ('path', 'split')):
            raise ValueError('session 需要 path/split')
        split = entry['split']
        if split not in SPLITS:
            raise ValueError('split 必须为 train/validation/test')
        phase = entry.get('phase', 'unspecified')
        if phase not in ('steady', 'transient', 'unspecified'):
            raise ValueError('phase 必须为 steady/transient/unspecified')
        folder = (manifest.parent / entry['path']).resolve()
        experiment_id = entry.get('experiment_id') or folder.name
        condition_id = entry.get('condition_id') or ''
        refmeta, dutmeta = read_json(folder / 'session.json'), read_json(folder / 'dut_session.json')
        if any(m.get('status') == 'running' for m in (refmeta, dutmeta)):
            raise ValueError(f'{folder} 会话尚未正常关闭；请先停止采集并确认日志完整')
        interval = float(refmeta['interval_seconds'])
        if not math.isfinite(interval) or interval <= 0 or interval != float(dutmeta['interval_seconds']):
            raise ValueError('REF/DUT 采样间隔无效或不同')
        ref_id, dut_id = refmeta['device_id'], dutmeta['device_id']
        ref, refbad = read_events(folder / 'raw.jsonl', ref_id, 'reference')
        dut, dutbad = read_events(folder / 'dut_raw.jsonl', dut_id, 'dut')
        if not ref or not dut:
            raise ValueError(f'{folder} 缺少有效 REF/DUT 广播')
        hashes = {name: raw_fingerprint(folder / name) for name in ('raw.jsonl', 'dut_raw.jsonl')}
        sid = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        if any(s['session_id'] == sid or s['path'] == str(folder) for s in sources):
            raise ValueError('清单包含重复会话')
        source = dict(entry, path=str(folder), reference=ref_id, dut=dut_id, hashes=hashes,
                      experiment_id=experiment_id, condition_id=condition_id,
                      session_id=sid, utc_start=min(x['utc'] for x in ref+dut),
                      utc_end=max(x['utc'] for x in ref+dut), interval_seconds=interval)
        assert_independent([s for s in sources if s['split'] != split], [source])
        sources.append(source)
        anchor = min(ref[0]['mono'], dut[0]['mono'])
        exclusions = []
        for exclusion in entry.get('exclude', []):
            start, end = float(exclusion['start']), float(exclusion['end'])
            if not math.isfinite(start+end) or start < 0 or end <= start or not exclusion.get('reason'):
                raise ValueError('exclude 需要有效 start/end 秒数和 reason')
            exclusions.append((anchor+start, anchor+end))
        reboot = [b['mono'] for a, b in zip(dut, dut[1:]) if b['time_uptime'] < a['time_uptime']]
        boundaries = sorted(set(reboot + [v for pair in exclusions for v in pair]))
        def sample(events):
            buckets = {}
            for row in events:
                if any(a <= row['mono'] < b for a, b in exclusions):
                    continue
                segment = bisect_right(boundaries, row['mono'])
                row['segment'] = segment
                buckets[(segment, int((row['mono'] - anchor) // interval))] = row
            return list(buckets.values())
        refs, duts = sample(ref), sample(dut)
        rows = []
        ref_segments = {}
        for r in refs:
            ref_segments.setdefault(r['segment'], []).append(r)
        segment_times = {key: [r['mono'] for r in values] for key, values in ref_segments.items()}
        for d in duts:
            candidates = ref_segments.get(d['segment'], [])
            if not candidates:
                continue
            # Binary search avoids quadratic work for long collection sessions.
            times = segment_times[d['segment']]
            index = bisect_right(times, d['mono'])
            near = candidates[max(0, index-1):index+1]
            r = min(near, key=lambda x: (abs(x['mono']-d['mono']), x['mono']))
            delta = abs(r['mono'] - d['mono'])
            if delta > gap:
                continue
            row = {k: v for k, v in d.items() if k not in ('mono', 'utc', 'line', 'segment')}
            row.update(sample_id=f"{sid}:{d['line']}", session_id=sid,
                       experiment_id=experiment_id, condition_id=condition_id,
                       phase=phase, split=split, device_id=dut_id, ref_device_id=ref_id,
                       dut_line=d['line'], ref_line=r['line'], dut_monotonic=d['mono'],
                       ref_monotonic=r['mono'], ref_received_at=r['received_at'],
                       ref_temperature=r['temperature'], ref_humidity=r['humidity'],
                       pair_gap_seconds=delta, quality='valid')
            rows.append(row)
        if not rows:
            raise ValueError(f'{folder} 没有容差内有效配对')
        reuse = Counter(row['ref_line'] for row in rows)
        for row in rows:
            row['ref_reuse_count'] = reuse[row['ref_line']]
            row['sample_weight'] = 1 / reuse[row['ref_line']]
        frames.append(pd.DataFrame(rows))
        quality[split].append(dict(session_id=sid, raw_reference=len(ref)+refbad, raw_dut=len(dut)+dutbad,
                                  invalid_reference=refbad, invalid_dut=dutbad,
                                  sampled_reference=len(refs), sampled_dut=len(duts), paired=len(rows),
                                  unpaired_dut=len(duts)-len(rows), reboot_count=len(reboot),
                                  excluded_reference=sum(any(a <= x['mono'] < b for a,b in exclusions) for x in ref),
                                  excluded_dut=sum(any(a <= x['mono'] < b for a,b in exclusions) for x in dut),
                                  reused_pairs=sum(v > 1 for v in [reuse[r['ref_line']] for r in rows]),
                                  max_pair_gap_seconds=max(r['pair_gap_seconds'] for r in rows),
                                  median_pair_gap_seconds=float(np.median([r['pair_gap_seconds'] for r in rows])),
                                  p95_pair_gap_seconds=float(np.percentile([r['pair_gap_seconds'] for r in rows], 95)),
                                  domain_exclusions=domain_exclusions(pd.DataFrame(rows), gap)))
    combined = pd.concat(frames, ignore_index=True)
    with overwritable_directory(output) as target:
        files = {}
        for split in SPLITS:
            frame = combined[combined.split == split]
            if not frame.empty:
                frame.to_csv(target / f'{split}.csv', index=False)
                files[split] = digest(target / f'{split}.csv')
        write_json(target / 'manifest.json', dict(schema_version=1, sources=sources, files=files,
                   alignment={'max_gap_seconds': gap}, units=UNITS | {'ref_temperature': 'degC', 'ref_humidity': '%RH', 'pair_gap_seconds': 's'}, coverage=spec.get('coverage', {})))
        write_json(target / 'quality.json', quality)
    return {s: int((combined.split == s).sum()) for s in SPLITS}


def load_split(path, split):
    path = Path(path)
    meta = read_json(path / 'manifest.json')
    if meta.get('schema_version') != 1 or split not in meta['files']:
        raise ValueError(f'数据集缺少 {split} 或版本不支持')
    file = path / f'{split}.csv'
    if digest(file) != meta['files'][split]:
        raise ValueError(f'{split} 数据指纹不匹配，请重新构建数据集')
    frame = pd.read_csv(file)
    if frame.empty or not frame['split'].eq(split).all():
        raise ValueError(f'{split} 集合为空或标签错误')
    for key in ('ref_temperature', 'ref_humidity', 'sample_weight'):
        if not np.isfinite(frame[key]).all():
            raise ValueError(f'{key} 包含无效值')
    return frame, meta
