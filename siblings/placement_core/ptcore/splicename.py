from __future__ import annotations
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Tuple
BPM_MIN = 60
BPM_MAX = 200
BPM_MAX_INDEX = 4
CONFIDENCE_HIGH = 'high'
CONFIDENCE_MEDIUM = 'medium'
CONFIDENCE_LOW = 'low'
CONFIDENCE_NONE = 'none'
_SHARP_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
_PITCH_CLASS = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
_MINOR_WORDS = {'min', 'minor', 'm'}
_MAJOR_WORDS = {'maj', 'major'}
_DYNAMICS = {'f', 'ff', 'fff', 'p', 'pp', 'ppp', 'mf', 'mp', 'sfz'}
_ONE_SHOT_HINTS = ('oneshot', 'one_shot', 'one shot', 'hit', 'stab')
_LOOP_HINTS = ('loop', 'break', 'groove', 'riff', 'songstarter', 'topline', 'fill', 'roll')
_BPM_SUFFIX_RE = re.compile('^(\\d{2,3})\\s*bpm$', re.IGNORECASE)
PREFIX_MAX_LEN = 7
_FX_HINTS = ('fx', 'sfx', 'riser', 'downlifter', 'sweep', 'impact', 'transition')

def normalize_key(root: str, accidental: str='') -> Tuple[str, int]:
    root = root.upper()
    if root not in _PITCH_CLASS:
        raise ValueError(f'音名として解釈できません: {root!r}')
    pc = _PITCH_CLASS[root]
    acc = accidental.lower()
    if acc in ('#', 's', 'sharp'):
        pc += 1
    elif acc in ('b', 'flat'):
        pc -= 1
    pc %= 12
    return (_SHARP_NAMES[pc], pc)

def semitones_between(from_key: str, to_key: str) -> int:
    _, a = _parse_key_text(from_key)
    _, b = _parse_key_text(to_key)
    diff = (b - a) % 12
    return diff - 12 if diff > 6 else diff

def _parse_key_text(text: str) -> Tuple[str, int]:
    m = re.match('^\\s*([A-Ga-g])\\s*(#|b|s|sharp|flat)?', text)
    if not m:
        raise ValueError(f'キーとして解釈できません: {text!r}')
    return normalize_key(m.group(1), m.group(2) or '')

def tempo_ratio(from_bpm: float, to_bpm: float) -> Fraction:
    if from_bpm <= 0 or to_bpm <= 0:
        raise ValueError('BPM は正の値である必要があります')
    return Fraction(from_bpm).limit_denominator(10 ** 6) / Fraction(to_bpm).limit_denominator(10 ** 6)

@dataclass(frozen=True)
class Token:
    text: str
    sep_before: str

def tokenize(stem: str) -> List[Token]:
    tokens: List[Token] = []
    buf = ''
    sep = ''
    pending_sep = ''
    for ch in stem:
        if ch in '_-. ':
            if buf:
                tokens.append(Token(buf, sep))
                sep = pending_sep or ch
                buf = ''
                pending_sep = ''
            pending_sep = ch
            sep = ch
        else:
            buf += ch
    if buf:
        tokens.append(Token(buf, sep))
    if tokens:
        tokens[0] = Token(tokens[0].text, '')
    return tokens

@dataclass
class ParsedSample:
    filename: str
    stem: str = ''
    bpm: Optional[int] = None
    bpm_confidence: str = CONFIDENCE_NONE
    key: Optional[str] = None
    key_quality: Optional[str] = None
    key_confidence: str = CONFIDENCE_NONE
    pitch_class: Optional[int] = None
    kind: Optional[str] = None
    prefix: Optional[str] = None
    description: str = ''
    tokens: List[str] = field(default_factory=list)

    @property
    def key_label(self) -> str:
        if not self.key:
            return '不明'
        if self.key_quality == 'minor':
            return f'{self.key}マイナー'
        if self.key_quality == 'major':
            return f'{self.key}メジャー'
        return self.key

    def summary(self) -> str:
        parts = [self.description or self.stem]
        if self.bpm:
            parts.append(f'{self.bpm} BPM')
        if self.key:
            parts.append(self.key_label)
        if self.kind:
            labels = {'loop': 'ループ', 'one_shot': 'ワンショット', 'fx': '効果音'}
            parts.append(labels[self.kind])
        return '、'.join(parts)

def _find_bpm(tokens: List[Token], stem_lower: str) -> Tuple[Optional[int], str, Optional[int]]:
    for i, tok in enumerate(tokens):
        m = _BPM_SUFFIX_RE.match(tok.text)
        if m and BPM_MIN <= int(m.group(1)) <= BPM_MAX:
            return (int(m.group(1)), CONFIDENCE_HIGH, i)
        if tok.text.isdigit() and i + 1 < len(tokens) and (tokens[i + 1].text.lower() == 'bpm') and (BPM_MIN <= int(tok.text) <= BPM_MAX):
            return (int(tok.text), CONFIDENCE_HIGH, i)
    sfx_id_style = any((t.sep_before == '.' and t.text.isdigit() for t in tokens))
    has_loop_vocab = any((h in stem_lower for h in _LOOP_HINTS))
    last_index = len(tokens) - 1
    for i, tok in enumerate(tokens):
        if not tok.text.isdigit():
            continue
        if len(tok.text) > 1 and tok.text[0] == '0':
            continue
        if not 2 <= len(tok.text) <= 3:
            continue
        value = int(tok.text)
        if not BPM_MIN <= value <= BPM_MAX:
            continue
        if tok.sep_before == '.':
            continue
        if sfx_id_style and (i == last_index or i > BPM_MAX_INDEX):
            continue
        prev_short = i == 0 or len(tokens[i - 1].text) <= PREFIX_MAX_LEN
        positional = prev_short and i <= BPM_MAX_INDEX and (last_index - i >= 2)
        confidence = CONFIDENCE_HIGH if positional or has_loop_vocab else CONFIDENCE_MEDIUM
        return (value, confidence, i)
    return (None, CONFIDENCE_NONE, None)
_KEY_WITH_QUALITY_RE = re.compile('^([A-G])(#|b)?(maj|min|major|minor|m|Maj|Min|Major|Minor)$')
_KEY_BARE_RE = re.compile('^([A-G])(#|b)?$')

def _find_key(tokens: List[Token], bpm_index: Optional[int]=None) -> Tuple[Optional[str], Optional[str], str, Optional[int]]:
    n = len(tokens)
    if n == 0:
        return (None, None, CONFIDENCE_NONE, None)

    def position_ok(i: int) -> bool:
        if i == n - 1:
            return True
        if i == n - 2 and (not tokens[n - 1].text.isdigit()):
            return True
        if bpm_index is not None and i == bpm_index + 1:
            return True
        if 0 < i <= 3 and all((len(t.text) <= PREFIX_MAX_LEN for t in tokens[:i])):
            return True
        return False
    for i in range(n - 1, -1, -1):
        m = _KEY_WITH_QUALITY_RE.match(tokens[i].text)
        if m and position_ok(i):
            root, acc, qual = (m.group(1), m.group(2) or '', m.group(3))
            key, pc = normalize_key(root, acc)
            q = qual.lower()
            quality = 'minor' if q in _MINOR_WORDS else 'major' if q in _MAJOR_WORDS else None
            conf = CONFIDENCE_HIGH if len(qual) >= 3 else CONFIDENCE_MEDIUM
            return (key, quality, conf, pc)
    for i in range(n - 1, -1, -1):
        tok = tokens[i]
        if tok.text in _DYNAMICS:
            continue
        m = _KEY_BARE_RE.match(tok.text)
        if m and position_ok(i):
            key, pc = normalize_key(m.group(1), m.group(2) or '')
            return (key, None, CONFIDENCE_MEDIUM, pc)
    return (None, None, CONFIDENCE_NONE, None)

def _find_kind(stem_lower: str, bpm: Optional[int]) -> Optional[str]:
    normalized = stem_lower.replace('-', '_').replace(' ', '_')
    if any((h.replace(' ', '_') in normalized for h in _ONE_SHOT_HINTS)):
        return 'one_shot'
    if any((h in normalized for h in _LOOP_HINTS)):
        return 'loop'
    if any((re.search(f'(?<![a-z]){h}(?![a-z])', normalized) for h in _FX_HINTS)):
        return 'fx'
    if bpm is not None:
        return 'loop'
    return None

def parse(filename: str) -> ParsedSample:
    name = filename.replace('\\', '/').rsplit('/', 1)[-1]
    stem = re.sub('\\.(wav|aif|aiff|mid|midi|flac|mp3)$', '', name, flags=re.IGNORECASE)
    tokens = tokenize(stem)
    result = ParsedSample(filename=filename, stem=stem, tokens=[t.text for t in tokens])
    bpm, bpm_conf, bpm_index = _find_bpm(tokens, stem.lower())
    result.bpm = bpm
    result.bpm_confidence = bpm_conf
    key, quality, key_conf, pc = _find_key(tokens, bpm_index)
    result.key = key
    result.key_quality = quality
    result.key_confidence = key_conf
    result.pitch_class = pc
    result.kind = _find_kind(stem.lower(), bpm)
    if bpm_index is not None and bpm_index > 0:
        prefix_tokens = [t.text for t in tokens[:bpm_index]]
    else:
        prefix_tokens = []
        for t in tokens:
            if len(t.text) <= 6 and (t.text.isupper() or any((c.isdigit() for c in t.text))):
                prefix_tokens.append(t.text)
            else:
                break
        prefix_tokens = prefix_tokens[:3]
    result.prefix = '_'.join(prefix_tokens) if prefix_tokens else None
    skip = set(prefix_tokens)
    desc = []
    for i, t in enumerate(tokens):
        if i == bpm_index:
            continue
        if t.text in skip:
            skip.discard(t.text)
            continue
        if key and (_KEY_WITH_QUALITY_RE.match(t.text) or (i == len(tokens) - 1 and _KEY_BARE_RE.match(t.text))):
            continue
        desc.append(t.text)
    result.description = ' '.join(desc).strip()
    return result

def parse_many(filenames: List[str]) -> List[ParsedSample]:
    return [parse(f) for f in filenames]
