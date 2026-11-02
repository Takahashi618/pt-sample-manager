"""配置前の加工（移調・タイムストレッチ）。

借りている部品（ptcore）の 3 つを繋ぐだけの層。

  splicename  ファイル名から素材の BPM とキーを読む
  prep        セッションの条件と突き合わせ、何半音・何倍にするか決める
  dsp         実際に加工する（ffmpeg か rubberband）

**PTSL には AudioSuite も Elastic Audio も無い**ので、音の加工は
Pro Tools の外でやるしかない。加工したファイルを取り込んで配置する。

部品が無い環境でもこのアプリは動く。その場合
`available()` が False を返し、加工なしで配置される。
"""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from core.i18n import COMMA, plural, tr
from core.models import channel_label
from core.parts import add_ptcore_to_path, part_message, parts_path
from core.session_settings import compose_key, key_name

# 借りている部品を読めるようにする（探し方は core/parts.py）
PARTS_PATH = parts_path()

# 加工したファイルの置き場所。取り込むと Pro Tools が自分の Audio Files へ
# コピーするので、ここに残しておく必要はない。
# 既定は一時ディレクトリだが、アプリのデータ領域を使うほうが確実なので
# app.py から set_work_dir() で差し替える
WORK_DIR = os.path.join(tempfile.gettempdir(), "sample_manager_render")


def set_work_dir(path: str) -> None:
    """加工したファイルの置き場所を変える。"""
    global WORK_DIR
    WORK_DIR = str(path)


# 加工の同時実行を防ぐ。**配置のワーカーと試聴のワーカーは同時に走り得る**
# （配置中にセッション条件を開いて試聴すると起きる）。同じ素材・同じ条件は
# 同じ出力パスになるので、2 つの ffmpeg が同じファイルへ書くと壊れる。
# 結果は決定的なので、直列にして待たせるだけでよい
_RENDER_LOCK = threading.Lock()


@dataclass
class RenderOutcome:
    """加工 1 回分の結果。

    `path` は配置に渡すファイル。加工しなかった場合は元のファイルのまま。
    """

    path: str
    changed: bool = False          # 実際に音を書き換えたか
    semitones: int = 0
    ratio: float = 1.0
    source_bpm: Optional[int] = None
    source_key: str = ""
    backend: str = ""
    notes: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def describe_ja(self) -> str:
        """読み上げ向けの 1 行。加工しなかったときは空文字。"""
        if self.error:
            return tr(f"加工できませんでした（{self.error}）",
                      f"Could not process the audio ({self.error})")
        if not self.changed:
            return ""
        parts = []
        if self.semitones:
            direction = tr("上げ",
                           "up") if self.semitones > 0 else tr("下げ", "down")
            steps = abs(self.semitones)
            parts.append(tr(f"{steps} 半音{direction}",
                            f"{direction} {plural(steps, 'semitone')}"))
        if self.ratio != 1.0:
            parts.append(tr(f"尺を {self.ratio:.3f} 倍",
                            f"length times {self.ratio:.3f}"))
        origin = []
        if self.source_bpm:
            origin.append(f"{self.source_bpm} BPM")
        if self.source_key:
            origin.append(self.source_key)
        head = ""
        if origin:
            source = COMMA.join(origin)
            head = tr(f"素材は{source}。", f"The sample is {source}. ")
        done = COMMA.join(parts)
        return tr(f"{head}{done}に加工しました。", f"{head}Processed: {done}.")


def _no_parts() -> str:
    return tr(f"借りている部品（ptcore）が見つかりません（{PARTS_PATH}）",
              f"The bundled part (ptcore) was not found ({PARTS_PATH})")


def _key_label(parsed) -> str:
    """素材のキーの読み上げ。長短が分からなければ根音だけ。"""
    if not parsed.key:
        return ""
    if parsed.key_quality in ("major", "minor"):
        return key_name(parsed.key, parsed.key_quality == "minor")
    return parsed.key


def _load():
    """借りている部品を読む。無ければ None。"""
    if not add_ptcore_to_path(PARTS_PATH):
        return None
    try:
        import dsp
        import prep
        import splicename
    except ImportError:
        return None
    return splicename, prep, dsp


def available() -> bool:
    """加工できる状態か（借りている部品と加工の道具が両方ある）。"""
    parts = _load()
    if parts is None:
        return False
    return parts[2].choose_backend().available()


def describe_backend() -> str:
    """使える加工の道具を 1 行で。設定ダイアログでの案内に使う。"""
    parts = _load()
    if parts is None:
        return tr(f"借りている部品（ptcore）が見つかりません（{PARTS_PATH}）。移調とタイムストレッチは使えません。",
                  f"The bundled part (ptcore) was not found ({PARTS_PATH}). "
                  "Transposing and time stretching are not available.")
    backend = parts[2].choose_backend()
    if not backend.available():
        return tr("ffmpeg も rubberband も入っていないため、移調とタイムストレッチは使えません。brew "
                  "install ffmpeg で入ります。",
                  "Neither ffmpeg nor rubberband is installed, so "
                  "transposing and time stretching are not available. "
                  "Install with: brew install ffmpeg.")
    if backend.name == "ffmpeg":
        return tr("加工に ffmpeg を使います。brew install rubberband を入れると音質が上がります。",
                  "Processing uses ffmpeg. Installing rubberband (brew "
                  "install rubberband) improves the sound quality.")
    return tr(f"加工に {backend.name} を使います。", f"Processing uses {backend.name}.")


@dataclass
class SampleMusic:
    """素材の音楽的な情報（ファイル名から読んだもの）。"""

    bpm: Optional[int] = None
    key: str = ""            # "Am" のような、そのまま設定に使える表記
    key_label: str = ""      # "Aマイナー" のような読み上げ向けの表記
    confident: bool = False  # 確信度が high かどうか

    @property
    def known(self) -> bool:
        return self.bpm is not None or bool(self.key)

    def describe_ja(self) -> str:
        """読み上げ向けの短い説明。分からなければ空文字。"""
        parts = []
        if self.bpm is not None:
            parts.append(f"{self.bpm} BPM")
        if self.key_label:
            parts.append(self.key_label)
        return COMMA.join(parts)


def read_sample_music(filename: str) -> SampleMusic:
    """ファイル名から素材の BPM とキーを読む。

    Splice の命名は流儀が複数あるが、実ライブラリ 564 件での検出率は
    BPM 278 件・キー 184 件で誤検出ゼロ（作者の調査）。

    **読めなくても失敗ではない。** 分からない素材のほうが多い。
    """
    parts = _load()
    if parts is None:
        return SampleMusic()
    splicename = parts[0]

    try:
        parsed = splicename.parse(filename)
    except Exception:
        return SampleMusic()

    return SampleMusic(
        bpm=parsed.bpm,
        # 設定にそのまま入れられる形にする（"A" + "m" など）
        key=compose_key(parsed.key or "", parsed.key_quality == "minor"),
        key_label=_key_label(parsed),
        confident=(parsed.bpm_confidence == "high"
                   or parsed.key_confidence == "high"),
    )


def validate_key(text: str) -> str:
    """キーの書き方を確かめる。問題なければ空文字を返す。"""
    parts = _load()
    if parts is None:
        return ""
    try:
        parts[1].key_label(text)
    except ValueError as exc:
        return tr(f"{exc}（例: Am、C#maj、F）",
                  f"{part_message(str(exc))} (examples: Am, C#maj, F)")
    return ""


def loop_positions(start: str, bpm: float, every_bars: float, copies: int,
                   sample_rate: int = 48000) -> List[str]:
    """ループを並べる位置を作る（小節・拍）。

    借りている部品の `placement.loop_positions` を使う。**テンポマップと
    拍子マップに対応している**ので、端数の小節（8.5 小節のドラムフィル等）
    でも正しい位置になる。

    借りている部品が無い、または計算できない場合は空リストを返す
    （呼び出し側が単純な足し算に落とす）。
    """
    if not add_ptcore_to_path(PARTS_PATH):
        return []
    try:
        import placement
        from timeline import SessionClock
    except ImportError:
        return []

    try:
        clock = SessionClock(sample_rate=sample_rate, tempo=bpm)
        return placement.loop_positions(
            clock, start, bars=every_bars, copies=copies)
    except (ValueError, TypeError):
        return []


def find_overlaps(positions: Sequence[str], bpm: float, duration: float,
                  sample_rate: int = 48000) -> List[str]:
    """並べた位置どうしが重なるかを調べる。

    **間隔より素材が長いと、後から置いたクリップが前のものを食う**
    （実機で確認済み）。戻り値は食われる位置の一覧。
    """
    if not positions or duration <= 0 or not add_ptcore_to_path(PARTS_PATH):
        return []
    try:
        import placement
        from timeline import SessionClock
    except ImportError:
        return []

    try:
        clock = SessionClock(sample_rate=sample_rate, tempo=bpm)
        return placement.find_self_overlaps(positions, clock, duration)
    except (ValueError, TypeError):
        return []


@dataclass
class _Entry:
    """`prep.plan_prep` に渡すための入れ物。

    借りている部品の `SampleEntry` と同じ属性を持てばよい
    （plan_prep はダックタイピングで受ける）。ファイル名から読んだ
    BPM・キーと、音声ヘッダから来たチャンネル数などを合わせて作る。
    """

    filename: str = ""
    bpm: Optional[int] = None
    bpm_confidence: str = "none"
    key: Optional[str] = None
    key_quality: Optional[str] = None
    key_confidence: str = "none"
    pitch_class: Optional[int] = None
    kind: Optional[str] = None
    channels: int = 0
    sample_rate: int = 0
    duration: float = 0.0


def plan_only(path: str, filename: str, settings, channels: Optional[int],
              sample_rate: Optional[int], duration: Optional[float]):
    """加工の計画だけを返す（実行しない）。

    UI で「何をするつもりか」を先に読み上げたいときに使う。
    借りている部品が無い、またはファイル名を解析できなければ None。
    """
    parts = _load()
    if parts is None:
        return None
    splicename, prep, _ = parts

    # read_sample_music と同じ防御。想定外のファイル名で
    # 例外が UI まで抜けないようにする
    try:
        parsed = splicename.parse(filename)
    except Exception:
        return None
    entry = _Entry(
        filename=filename,
        bpm=parsed.bpm,
        bpm_confidence=parsed.bpm_confidence,
        key=parsed.key,
        key_quality=parsed.key_quality,
        key_confidence=parsed.key_confidence,
        pitch_class=parsed.pitch_class,
        kind=parsed.kind,
        channels=int(channels or 0),
        sample_rate=int(sample_rate or 0),
        duration=float(duration or 0.0),
    )
    target = prep.Target(
        sample_rate=settings.sample_rate,
        channels=int(channels or 2),
        bpm=settings.bpm,
        key=settings.key or None,
    )
    return prep.plan_prep(entry, target), parsed


def render_for_session(path: str, filename: str, settings,
                       channels: Optional[int] = None,
                       sample_rate: Optional[int] = None,
                       duration: Optional[float] = None) -> RenderOutcome:
    """セッションの条件に合わせて素材を加工する。

    **加工が要らない場合は元のパスをそのまま返す**（`changed=False`）。
    呼び出し側は結果の `path` を配置に渡せばよい。

    失敗しても例外は投げない。`ok` を見ること。**加工に失敗したときは
    元のファイルを返さない**（意図しない音程・尺で置いてしまうため）。
    """
    if not settings.can_render:
        return RenderOutcome(path=path)

    parts = _load()
    if parts is None:
        return RenderOutcome(
            path=path,
            error=_no_parts())
    _, _, dsp = parts

    # **計画づくりの例外もここで受ける。** 設定ファイルに手で書いたキーなど、
    # 借りている部品が解釈できない値は ValueError になる。「失敗しても例外は
    # 投げない」の約束を守り、理由を文言に残す（配置なら「予期しない
    # エラー」、試聴なら「準備に失敗」としか読まれなかった）
    try:
        planned = plan_only(path, filename, settings,
                            channels, sample_rate, duration)
    except Exception as exc:
        return RenderOutcome(
            path=path, error=tr(f"加工の計画を作れませんでした（{exc}）",
                                "Could not plan the processing "
                                f"({part_message(str(exc))})"))
    if planned is None:
        return RenderOutcome(path=path, error=tr("加工の計画を作れませんでした",
                                                 "Could not plan the "
                                                 "processing"))
    plan, parsed = planned

    outcome = RenderOutcome(
        path=path,
        semitones=plan.semitones,
        ratio=float(plan.ratio),
        source_bpm=parsed.bpm,
        source_key=_key_label(parsed),
        notes=list(plan.confirmations) + list(plan.warnings),
    )

    if not plan.destructive:
        # 移調もストレッチも要らない。サンプルレートの違いは
        # Pro Tools が取り込み時に吸収するのでここでは触らない
        return outcome

    if not dsp.choose_backend().available():
        outcome.error = tr("移調とタイムストレッチに使える道具がありません。brew install ffmpeg "
                           "で入ります",
                           "No tool for transposing and time stretching was "
                           "found. Install with: brew install ffmpeg")
        return outcome

    dest = _work_path(filename, plan)
    with _RENDER_LOCK:
        result = dsp.process(path, dest, plan.semitones, float(plan.ratio))
    if not result.ok:
        outcome.error = part_message(result.message)
        return outcome

    outcome.path = result.dest
    outcome.changed = not result.skipped
    outcome.backend = result.backend
    return outcome


@dataclass
class ChannelOutcome:
    """チャンネル数合わせの結果。"""

    path: str
    changed: bool = False
    from_channels: int = 0
    to_channels: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def lossy(self) -> bool:
        """音が変わる向きの変換か。

        ステレオ → モノラルは L と R を混ぜるので**音が変わる**。
        逆（モノラル → ステレオ）は複製するだけなので変わらない。
        """
        return self.changed and self.to_channels < self.from_channels

    def describe_ja(self) -> str:
        if self.error:
            return tr(f"チャンネル数を合わせられませんでした（{self.error}）",
                      f"Could not match the channel count ({self.error})")
        if not self.changed:
            return ""
        src = channel_label(self.from_channels)
        dst = channel_label(self.to_channels)
        if self.lossy:
            return tr(f"トラックに合わせて{src}を{dst}にまとめました。左右が混ざるため音が変わります。",
                      f"Mixed {src} down to {dst} to match the track. The "
                      "sound changes because left and right are mixed.")
        return tr(f"トラックに合わせて{src}を{dst}にしました。音は変わりません。",
                  f"Changed {src} to {dst} to match the track. The sound "
                  "does not change.")


def adapt_channels(path: str, filename: str,
                   target_channels: int) -> ChannelOutcome:
    """トラックのチャンネル構成に素材を合わせる。

    **Pro Tools はチャンネル構成が合わないクリップを置かせてくれない。**
    素材の側を合わせれば置けるようになる。

    すでに合っていれば何もしない（元のパスを返す）。
    """
    if not os.path.exists(path):
        return ChannelOutcome(path=path, error=tr(f"ファイルがありません: {path}",
                                                  "The file does not "
                                                  f"exist: {path}"))

    parts = _load()
    if parts is None:
        return ChannelOutcome(
            path=path,
            error=_no_parts())
    _, _, dsp = parts

    try:
        from audiofile import read_audio_info
    except ImportError:
        return ChannelOutcome(path=path, error=tr("音声ヘッダを読めません",
                                                  "Cannot read the audio "
                                                  "file header"))

    info = read_audio_info(path)
    current = info.channels if info else 0
    # チャンネル数が読めない素材は触らない（判定できないだけで、
    # 配置を妨げるほどではない。既存の適合判定と同じ考え方）
    if not current or not target_channels or current == target_channels:
        return ChannelOutcome(path=path, from_channels=current,
                              to_channels=target_channels or current)

    stem = os.path.splitext(os.path.basename(filename))[0]
    dest = os.path.join(WORK_DIR, f"{stem}__{target_channels}ch.wav")
    with _RENDER_LOCK:
        result = dsp.convert_channels(path, dest, target_channels)
    if not result.ok:
        return ChannelOutcome(path=path, from_channels=current,
                              to_channels=target_channels,
                              error=part_message(result.message))

    return ChannelOutcome(path=result.dest, changed=not result.skipped,
                          from_channels=current, to_channels=target_channels)


def _work_path(filename: str, plan) -> str:
    """加工したファイルの置き場所を決める。

    同じ素材・同じ加工なら同じ名前になるので、作り直しても散らからない。
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    tags = []
    if plan.needs_pitch:
        tags.append(f"{plan.semitones:+d}st")
    if plan.needs_stretch:
        tags.append(f"x{float(plan.ratio):.4f}")
    return os.path.join(WORK_DIR, f"{stem}__{'_'.join(tags) or 'copy'}.wav")
