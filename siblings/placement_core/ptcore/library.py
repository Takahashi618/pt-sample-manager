from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict, field
from typing import Dict, Iterable, List, Optional, Sequence
try:
    from .audiofile import AUDIO_EXTENSIONS, long_path, read_audio_info
    from .splicename import parse as parse_name, normalize_key
except ImportError:
    from audiofile import AUDIO_EXTENSIONS, long_path, read_audio_info
    from splicename import parse as parse_name, normalize_key
INDEX_VERSION = 1

@dataclass
class SampleEntry:
    path: str
    filename: str
    pack: str = ''
    size: int = 0
    mtime: float = 0.0
    description: str = ''
    bpm: Optional[int] = None
    bpm_confidence: str = 'none'
    key: Optional[str] = None
    key_quality: Optional[str] = None
    key_confidence: str = 'none'
    pitch_class: Optional[int] = None
    kind: Optional[str] = None
    channels: int = 0
    sample_rate: int = 0
    bit_depth: int = 0
    duration: float = 0.0
    codec: str = ''

    @property
    def key_label(self) -> str:
        if not self.key:
            return '不明'
        if self.key_quality == 'minor':
            return f'{self.key}マイナー'
        if self.key_quality == 'major':
            return f'{self.key}メジャー'
        return self.key

    @property
    def kind_label(self) -> str:
        return {'loop': 'ループ', 'one_shot': 'ワンショット', 'fx': '効果音'}.get(self.kind or '', '不明')

    @property
    def channel_label(self) -> str:
        return {1: 'モノラル', 2: 'ステレオ'}.get(self.channels, f'{self.channels}ch')

    def summary(self) -> str:
        parts = [self.description or self.filename]
        if self.bpm:
            parts.append(f'{self.bpm} BPM')
        if self.key:
            parts.append(self.key_label)
        parts.append(self.kind_label)
        parts.append(f'{self.duration:.1f}秒')
        return '、'.join(parts)

    def detail(self) -> Dict[str, str]:
        d = {'説明': self.description, 'パック': self.pack, '種別': self.kind_label, '長さ': f'{self.duration:.2f} 秒', '形式': f'{self.sample_rate} Hz {self.bit_depth} bit {self.channel_label}', 'ファイル名': self.filename}
        if self.bpm:
            note = '' if self.bpm_confidence == 'high' else '（要確認）'
            d['BPM'] = f'{self.bpm}{note}'
        if self.key:
            note = '' if self.key_confidence == 'high' else '（要確認）'
            d['キー'] = f'{self.key_label}{note}'
        if self.codec and self.codec != 'PCM':
            d['コーデック'] = self.codec
        return d

def relative_key(pitch_class: int, quality: Optional[str]) -> Optional[tuple]:
    if quality == 'minor':
        return ((pitch_class + 3) % 12, 'major')
    if quality == 'major':
        return ((pitch_class - 3) % 12, 'minor')
    return None

def relative_pitch_class(pitch_class: int, quality: Optional[str]) -> Optional[int]:
    rel = relative_key(pitch_class, quality)
    return rel[0] if rel else None

def parse_key_query(text: str) -> tuple:
    import re
    m = re.match('^\\s*([A-Ga-g])\\s*(#|b|s)?\\s*(maj|min|major|minor|m|M)?\\s*$', text)
    if not m:
        raise ValueError(f'キーとして解釈できません: {text!r}')
    _, pc = normalize_key(m.group(1), m.group(2) or '')
    q = (m.group(3) or '').lower()
    if q in ('min', 'minor', 'm'):
        quality = 'minor'
    elif q in ('maj', 'major'):
        quality = 'major'
    else:
        quality = None
    return (pc, quality)

def bpm_matches(value: Optional[int], target: int, tolerance: int=0, half_double: bool=False) -> bool:
    if value is None:
        return False
    candidates = [target]
    if half_double:
        candidates += [target * 2, target // 2]
    return any((abs(value - c) <= tolerance for c in candidates))

class Library:

    def __init__(self, root: str, index_path: Optional[str]=None):
        self.root = os.path.abspath(root)
        self.index_path = index_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'index.json')
        self.entries: List[SampleEntry] = []

    def scan(self, force: bool=False) -> Dict[str, int]:
        known: Dict[str, SampleEntry] = {}
        if not force and self.load(quiet=True):
            known = {e.path: e for e in self.entries}
        entries: List[SampleEntry] = []
        stats = {'total': 0, 'reused': 0, 'parsed': 0, 'unreadable': 0}
        for dirpath, _, filenames in os.walk(self.root):
            for fn in filenames:
                if fn.startswith('.') or os.path.splitext(fn)[1].lower() not in AUDIO_EXTENSIONS:
                    continue
                path = os.path.join(dirpath, fn)
                stats['total'] += 1
                try:
                    st = os.stat(long_path(path))
                except OSError:
                    stats['unreadable'] += 1
                    continue
                old = known.get(path)
                if old and old.size == st.st_size and (old.mtime == st.st_mtime):
                    entries.append(old)
                    stats['reused'] += 1
                    if not old.sample_rate:
                        stats['unreadable'] += 1
                    continue
                entry = self._build_entry(path, st.st_size, st.st_mtime)
                if entry is None:
                    stats['unreadable'] += 1
                    continue
                entries.append(entry)
                stats['parsed'] += 1
                if not entry.sample_rate:
                    stats['unreadable'] += 1
        self.entries = entries
        self.save()
        return stats

    def _build_entry(self, path: str, size: int, mtime: float) -> Optional[SampleEntry]:
        fn = os.path.basename(path)
        p = parse_name(fn)
        rel = os.path.relpath(path, self.root)
        pack = rel.split(os.sep)[0] if os.sep in rel else ''
        e = SampleEntry(path=path, filename=fn, pack=pack, size=size, mtime=mtime, description=p.description, bpm=p.bpm, bpm_confidence=p.bpm_confidence, key=p.key, key_quality=p.key_quality, key_confidence=p.key_confidence, pitch_class=p.pitch_class, kind=p.kind)
        info = read_audio_info(path)
        if info:
            e.channels = info.channels
            e.sample_rate = info.sample_rate
            e.bit_depth = info.bit_depth
            e.duration = round(info.duration, 3)
            e.codec = info.codec
        return e

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.index_path)), exist_ok=True)
        payload = {'version': INDEX_VERSION, 'root': self.root, 'entries': [asdict(e) for e in self.entries]}
        tmp = f'{self.index_path}.{os.getpid()}.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.index_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self, quiet: bool=False) -> bool:
        try:
            with open(self.index_path, encoding='utf-8') as f:
                payload = json.load(f)
            if payload.get('version') != INDEX_VERSION:
                return False
            if payload.get('root', self.root) != self.root:
                return False
            entries = [SampleEntry(**d) for d in payload.get('entries', [])]
        except (OSError, ValueError, TypeError, AttributeError):
            if not quiet:
                raise
            return False
        self.entries = [e for e in entries if not e.filename.startswith('.')]
        return True

    def search(self, text: Optional[str]=None, bpm: Optional[int]=None, tolerance: int=0, half_double: bool=False, key: Optional[str]=None, relative: bool=False, kind: Optional[str]=None, pack: Optional[str]=None, sample_rate: Optional[int]=None, channels: Optional[int]=None, min_duration: Optional[float]=None, max_duration: Optional[float]=None, confident_only: bool=False, sort: str='name', limit: Optional[int]=None) -> List[SampleEntry]:
        results = list(self.entries)
        if text:
            words = [w.lower() for w in text.split() if w]

            def hit(e: SampleEntry) -> bool:
                haystack = f'{e.filename} {e.pack} {e.description}'.lower()
                return all((w in haystack for w in words))
            results = [e for e in results if hit(e)]
        if bpm is not None:
            results = [e for e in results if bpm_matches(e.bpm, bpm, tolerance, half_double)]
        if key:
            want_pc, want_quality = parse_key_query(key)

            def key_hit(e: SampleEntry) -> bool:
                if e.pitch_class is None:
                    return False
                if e.pitch_class == want_pc and (want_quality is None or e.key_quality is None or e.key_quality == want_quality):
                    return True
                if relative:
                    rel = relative_key(want_pc, want_quality)
                    if rel is not None:
                        rel_pc, rel_quality = rel
                        if e.pitch_class == rel_pc and (e.key_quality is None or e.key_quality == rel_quality):
                            return True
                return False
            results = [e for e in results if key_hit(e)]
        if kind:
            results = [e for e in results if e.kind == kind]
        if pack:
            needle = pack.lower()
            results = [e for e in results if needle in e.pack.lower()]
        if sample_rate is not None:
            results = [e for e in results if e.sample_rate == sample_rate]
        if channels is not None:
            results = [e for e in results if e.channels == channels]
        if min_duration is not None:
            results = [e for e in results if e.duration >= min_duration]
        if max_duration is not None:
            results = [e for e in results if e.duration <= max_duration]
        if confident_only:
            results = [e for e in results if (e.bpm is None or e.bpm_confidence == 'high') and (e.key is None or e.key_confidence == 'high')]
        keys = {'name': lambda e: e.filename.lower(), 'bpm': lambda e: (e.bpm is None, e.bpm or 0, e.filename.lower()), 'duration': lambda e: e.duration, 'pack': lambda e: (e.pack.lower(), e.filename.lower()), 'key': lambda e: (e.pitch_class is None, e.pitch_class or 0)}
        results.sort(key=keys.get(sort, keys['name']))
        return results[:limit] if limit else results

    def stats(self) -> Dict[str, object]:
        from collections import Counter
        n = len(self.entries)
        if not n:
            return {'件数': 0}
        rates = Counter((e.sample_rate for e in self.entries))
        kinds = Counter((e.kind_label for e in self.entries))
        packs = {e.pack for e in self.entries}
        with_bpm = [e for e in self.entries if e.bpm]
        with_key = [e for e in self.entries if e.key]
        durations = sorted((e.duration for e in self.entries))
        return {'件数': n, 'パック数': len(packs), 'BPM 判明': f'{len(with_bpm)} 件', 'キー判明': f'{len(with_key)} 件', '種別': '、'.join((f'{k} {v}' for k, v in kinds.most_common())), 'サンプルレート': '、'.join((f'{k} Hz {v} 件' for k, v in sorted(rates.items()))), '長さの中央値': f'{durations[n // 2]:.1f} 秒'}

    def packs(self) -> List[str]:
        return sorted({e.pack for e in self.entries if e.pack})

    def bpm_values(self) -> List[int]:
        return sorted({e.bpm for e in self.entries if e.bpm})

    def keys(self) -> List[str]:
        return sorted({e.key_label for e in self.entries if e.key})
