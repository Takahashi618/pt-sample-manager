from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
TRACK_LISTING_HEADER = 'T R A C K  L I S T I N G'
_TRACK_NAME_RE = re.compile('^(?P<name>.*?)(?:\\s+\\((?P<fmt>[^)]+)\\))?$')

@dataclass(frozen=True)
class EdlClip:
    channel: int
    event: int
    name: str
    start: str
    end: str
    duration: str
    state: str = ''

    @property
    def start_bar_beat(self) -> str:
        return normalize_bars_beats(self.start)

@dataclass
class TrackEdl:
    name: str
    track_format: str = ''
    clips: List[EdlClip] = field(default_factory=list)

    @property
    def events(self) -> int:
        return len({c.event for c in self.clips})

    def starts(self) -> List[str]:
        seen: List[str] = []
        for c in self.clips:
            bb = c.start_bar_beat
            if bb not in seen:
                seen.append(bb)
        return seen

@dataclass
class SessionInfo:
    headers: Dict[str, str] = field(default_factory=dict)
    tracks: List[TrackEdl] = field(default_factory=list)

    @property
    def session_name(self) -> str:
        return self.headers.get('SESSION NAME', '')

    @property
    def sample_rate(self) -> Optional[int]:
        raw = self.headers.get('SAMPLE RATE', '')
        try:
            return int(float(raw))
        except ValueError:
            return None

    def track(self, name: str) -> Optional[TrackEdl]:
        for t in self.tracks:
            if t.name == name:
                return t
        return None

    def tracks_with_clip(self, clip_name: str) -> List[str]:
        hits = []
        for t in self.tracks:
            if any((clip_name in c.name for c in t.clips)):
                hits.append(t.name)
        return hits

def normalize_bars_beats(value: str) -> str:
    parts = [p.strip() for p in str(value).split('|')]
    if len(parts) < 2:
        return str(value).strip()
    return f'{parts[0]}|{parts[1]}'

def parse_session_info(text: str) -> SessionInfo:
    info = SessionInfo()
    if not text:
        return info
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if TRACK_LISTING_HEADER in line:
            idx += 1
            break
        if '\t' in line and line.rstrip().endswith(':') is False:
            key, _, value = line.partition('\t')
            key = key.rstrip(':').strip()
            if key:
                info.headers[key] = value.strip()
        idx += 1
    current: Optional[TrackEdl] = None
    for line in lines[idx:]:
        stripped = line.strip()
        if line.startswith('TRACK NAME:'):
            name, fmt = _split_track_name(line.partition('\t')[2].strip())
            current = TrackEdl(name=name, track_format=fmt)
            info.tracks.append(current)
            continue
        if current is None or not stripped:
            continue
        if stripped.startswith(('COMMENTS:', 'USER DELAY:', 'STATE:', 'CHANNEL')):
            continue
        clip = _parse_clip_line(line)
        if clip is not None:
            current.clips.append(clip)
    return info

def _split_track_name(raw: str) -> tuple:
    m = _TRACK_NAME_RE.match(raw)
    if not m:
        return (raw, '')
    return (m.group('name').strip(), (m.group('fmt') or '').strip())

def _parse_clip_line(line: str) -> Optional[EdlClip]:
    cols = [c.strip() for c in line.split('\t')]
    if len(cols) < 6:
        return None
    try:
        channel = int(cols[0])
        event = int(cols[1])
    except ValueError:
        return None
    return EdlClip(channel=channel, event=event, name=cols[2], start=cols[3], end=cols[4], duration=cols[5], state=cols[6] if len(cols) > 6 else '')
