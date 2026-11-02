"""配置先セッションの条件（テンポ・キー・サンプルレート）。

**PTSL からはテンポもキーも取れない。** セッションのサンプルレートは
`GetSessionSampleRate` で取れるが、テンポは PTSL に問い合わせる手段が無い
（`TType_Tempo` はトラック種別、`tempo_meter_map` は取り込みオプションで、
どちらも別物）。そのため利用者に一度入力してもらい、ここに保持する。

この条件が入っていると、配置のたびに素材をセッションへ合わせられる
（移調とタイムストレッチ。`integration/render_service.py`）。
入っていなければ加工せずそのまま置く。**既定は「入っていない」。**

Pro Tools にも wx にも依存しない純粋なデータなので、単体テストできる。
"""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from core.i18n import COMMA, plural, tr

# 実用的な範囲。外れた値は打ち間違いとみなして弾く
MIN_BPM = 20.0
MAX_BPM = 400.0

# よく使うサンプルレート。ダイアログの選択肢に使う
COMMON_RATES = (44100, 48000, 88200, 96000, 192000)

DEFAULT_RATE = 48000

# 試聴の音量。**既定は少し絞る。** 素材によって音量差が大きく、
# いきなり最大で鳴ると耳に痛い
DEFAULT_VOLUME = 0.7

# 音量を上げ下げする刻み
VOLUME_STEP = 0.1


@dataclass
class SessionSettings:
    """配置先セッションの条件と、配置のしかた。

    bpm と key は「未設定」を None / 空文字で表す。**両方とも任意。**
    テンポだけ分かっていればストレッチ、キーだけなら移調ができる。
    """

    bpm: float | None = None
    key: str = ""                 # "Am" "C#maj" など。空なら未設定
    sample_rate: int = DEFAULT_RATE
    auto_render: bool = True      # 配置時に自動で加工するか
    # 試聴の音量（0.0〜1.0）。セッションの条件ではないが、**利用者の好みを
    # 覚えておく場所がここしかない**ので同じファイルに入れてある。
    # 終了時のクリア（テンポとキー）の対象にはしない
    preview_volume: float = DEFAULT_VOLUME

    # -- 配置のしかた -------------------------------------------------------
    # 位置の決め方。既定は編集カーソルの位置に置く
    at_cursor: bool = True
    # **カーソルの位置を拍に合わせるか。既定は合わせる。**
    # 画面を見ずに拍の上へカーソルを置くのは難しく、外れた位置に置くと
    # メトロノームと合わない（実機で確認: 拍の半分ずれると 187 ミリ秒）。
    # 拍の外へ意図して置きたいときだけ外す
    snap_to_beat: bool = True
    # at_cursor が偽のときに使う小節・拍（"17|1|000"）
    position: str = "1|1|000"
    # 並べる個数と間隔（小節）。copies が 1 なら 1 個だけ置く
    copies: int = 1
    every_bars: float = 4.0

    # -- 判定 ---------------------------------------------------------------

    @property
    def has_tempo(self) -> bool:
        return self.bpm is not None

    @property
    def has_key(self) -> bool:
        return bool(self.key)

    @property
    def can_render(self) -> bool:
        """加工の条件が揃っているか。

        テンポもキーも無ければ、合わせる先が無いので加工しようがない。
        """
        return self.auto_render and (self.has_tempo or self.has_key)

    @property
    def repeats(self) -> bool:
        """2 個以上並べる設定になっているか。"""
        return self.copies > 1

    @property
    def can_repeat(self) -> bool:
        """並べるのに必要な条件が揃っているか。

        **間隔を小節で数えるのでテンポが要る。** テンポが分からないと
        「4 小節おき」が何秒なのか決められない。
        """
        return self.repeats and self.has_tempo and self.every_bars > 0

    # -- 表示 ---------------------------------------------------------------

    def describe_ja(self) -> str:
        """読み上げ向けの 1 行。"""
        parts = []
        if self.has_tempo:
            parts.append(tr(f"テンポ {self.bpm:g}", f"tempo {self.bpm:g}"))
        else:
            parts.append(tr("テンポ未設定", "tempo not set"))
        # キーは「Aマイナー」の形で読む。"Am" のままだと読み上げが不定
        # （ダイアログ内の読み（describe_key_ja）とも揃える）
        parts.append(tr(f"キー {describe_key_ja(self.key)}",
                        f"key {describe_key_ja(self.key)}") if self.has_key
                     else tr("キー未設定", "key not set"))
        parts.append(f"{self.sample_rate} Hz")
        if self.at_cursor:
            parts.append(
                tr("カーソル位置へ（拍に合わせる）",
                   "at the cursor (snapped to the beat)") if self.snap_to_beat
                else tr("カーソル位置へ（拍に合わせない）",
                        "at the cursor (not snapped to the beat)"))
        else:
            parts.append(tr(f"{self.position} へ", f"at {self.position}"))
        if self.repeats:
            if self.can_repeat:
                parts.append(tr(f"{self.copies} 個を {self.every_bars:g} 小節おき",
                                f"{self.copies} copies every "
                                f"{plural(self.every_bars, 'bar')}"))
            else:
                parts.append(
                    tr(f"{self.copies} 個を並べる設定だが、テンポと間隔が要るため 1 個だけ置きます",
                       f"set to {self.copies} copies, but only 1 will be "
                       "placed because they need a tempo and an interval"))
        if not self.auto_render:
            parts.append(tr("自動加工オフ", "automatic processing off"))
        elif not self.can_render:
            parts.append(tr("加工の条件が足りないため、そのまま配置します",
                            "not enough settings to process, so samples "
                            "are placed as they are"))
        return COMMA.join(parts)

    def positions(self) -> list[str]:
        """並べる位置の一覧を作る（小節・拍）。

        カーソル位置に置く設定のときは空リストを返す。**位置を計算するのは
        小節・拍を指定した場合だけ**で、カーソル位置は実行時に
        Pro Tools から取る。

        借りている部品の `loop_positions` を使う（テンポマップ・拍子マップに
        対応している）。無い環境では単純な小節の足し算にする。
        """
        if self.at_cursor:
            return []
        if not self.can_repeat:
            return [self.position]

        try:
            from integration.render_service import loop_positions

            got = loop_positions(
                self.position, self.bpm, self.every_bars, self.copies,
                self.sample_rate)
            if got:
                return got
        except ImportError:
            pass

        return _simple_positions(self.position, self.every_bars, self.copies)

    # -- 永続化 -------------------------------------------------------------

    def save(self, path: Path) -> None:
        """JSON で書き出す。失敗しても例外は投げない。

        設定が保存できないことでアプリが止まるほうが困るため。
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    @classmethod
    def load(cls, path: Path) -> "SessionSettings":
        """読み込む。無い・壊れている場合は既定値を返す。"""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()

        bpm, _ = parse_bpm(data.get("bpm"))
        rate = data.get("sample_rate")
        position, position_error = parse_position(
            data.get("position") or "1|1|000")
        copies, _ = parse_copies(data.get("copies"))
        every_bars, _ = parse_every_bars(data.get("every_bars"))
        return cls(
            bpm=bpm,
            key=str(data.get("key") or "").strip(),
            sample_rate=int(rate) if isinstance(rate, int) and rate > 0
            else DEFAULT_RATE,
            auto_render=bool(data.get("auto_render", True)),
            preview_volume=parse_volume(data.get("preview_volume")),
            at_cursor=bool(data.get("at_cursor", True)),
            snap_to_beat=bool(data.get("snap_to_beat", True)),
            position="1|1|000" if position_error else position,
            copies=copies,
            every_bars=every_bars,
        )


def _simple_positions(start: str, every_bars: float,
                      copies: int) -> list[str]:
    """小節を足していくだけの控え。4/4 の整数小節を前提とする。

    借りている部品が無い環境向け。拍子の変化には対応しない。
    """
    parts = str(start).split("|")
    try:
        bar = int(parts[0])
    except (ValueError, IndexError):
        return [start]
    tail = "|".join(parts[1:]) if len(parts) > 1 else "1|000"
    step = max(1, int(round(every_bars)))
    return [f"{bar + step * i}|{tail}" for i in range(copies)]


def _typed(value) -> str:
    """入力欄の文字を読める形にそろえる（互換の正規化 NFKC と前後の空白）。

    **日本語入力のまま打つと全角になる。** 「１７｜１｜０００」や「４．５」は
    読み上げでは半角と区別がつかないのに、そのままでは弾かれる
    （全角の縦棒は区切りと見なされず、全角の小数点は float が受けない）。
    全角の数字だけは int() が受けるので、「１７|１|０００」は検証を通り、
    全角のまま Pro Tools へ渡って配置に失敗していた。
    NFKC で半角へそろえてから読む。
    """
    text = "" if value is None else str(value)
    return unicodedata.normalize("NFKC", text).strip()


def parse_position(value) -> tuple[str, str]:
    """小節・拍の入力を読む。戻り値は (値, エラー文言)。

    `17|1|000` や `17|1` のような形を受ける。空欄はエラー
    （位置を指定するモードで空欄だと置き場所が決まらないため）。

    返す値は**半角にそろえ、縦棒の前後の空白を除いた形**にする
    （そのまま Pro Tools と 借りている部品へ渡すため）。
    """
    text = _typed(value)
    if not text:
        return "", tr("位置を 17|1|000 のように入力してください",
                      "Enter a position like 17|1|000")
    parts = [part.strip() for part in text.split("|")]
    if len(parts) < 2:
        return "", tr(f"位置は 小節|拍|ティック の形で入力してください（{text}）",
                      f"Enter the position as bar|beat|tick ({text})")
    for part in parts:
        # isdigit は上付き数字（²）等も通すが int() は受けないので
        # isdecimal で確かめる。NFKC のあとなので全角の数字は残らないが、
        # ほかの書き方の数字（アラビア数字など）は ASCII かどうかで弾く
        if not (part.isdecimal() and part.isascii()):
            return "", tr(f"位置は数字と縦棒だけで入力してください（{text}）",
                          "Use only numbers and vertical bars in the "
                          f"position ({text})")
    if int(parts[0]) < 1 or int(parts[1]) < 1:
        return "", tr(f"小節と拍は 1 以上で入力してください（{text}）",
                      f"The bar and beat must be 1 or more ({text})")
    return "|".join(parts), ""


def parse_copies(value) -> tuple[int, str]:
    """並べる個数を読む。空欄は 1 個。"""
    text = _typed(value)
    if not text:
        return 1, ""
    try:
        copies = int(text)
    except ValueError:
        return 1, tr(f"個数は整数で入力してください（{text}）",
                     f"The number of copies must be a whole number ({text})")
    if copies < 1:
        return 1, tr(f"個数は 1 以上で入力してください（{copies}）",
                     f"The number of copies must be 1 or more ({copies})")
    if copies > 128:
        return 1, tr(f"個数は 128 以下で入力してください（{copies}）",
                     f"The number of copies must be 128 or less ({copies})")
    return copies, ""


def parse_every_bars(value) -> tuple[float, str]:
    """並べる間隔（小節）を読む。空欄は 4 小節。

    **無限大と非数は弾く。** float は "inf" や "1e400" を受けてしまい、
    配置のときに位置の計算で落ちて「予期しないエラー」とだけ読まれていた。
    """
    text = _typed(value)
    if not text:
        return 4.0, ""
    try:
        bars = float(text)
    except ValueError:
        return 4.0, tr(f"間隔は数値で入力してください（{text}）",
                       f"The interval must be a number ({text})")
    if not math.isfinite(bars):
        return 4.0, tr(f"間隔は数値で入力してください（{text}）",
                       f"The interval must be a number ({text})")
    if bars <= 0:
        return 4.0, tr(f"間隔は 0 より大きい値で入力してください（{bars:g}）",
                       f"The interval must be greater than 0 ({bars:g})")
    return bars, ""


# キーの選択肢。**上下矢印で選べるようにするための並び。**
# 半音ずつ上がる順にしてあるので、隣同士が音楽的にも隣になる
KEY_ROOTS = ("C", "C#", "D", "D#", "E", "F",
             "F#", "G", "G#", "A", "A#", "B")

# 長調・短調の別。表記は借りている部品に合わせる（短調だけ "m" が付く）
KEY_QUALITIES = ((tr("メジャー", "Major"), ""), (tr("マイナー", "Minor"), "m"))

# 「指定しない」を表す先頭項目
KEY_NONE = tr("指定しない", "Not specified")

# フラット表記 → 選択肢（シャープ表記）への読み替え。借りている部品は
# シャープ系に正規化するが、以前の自由入力欄では "Db" のような書き方も
# 保存できた。**黙って「指定しない」に倒すと設定が消える**ので、
# 同じ音の選択肢に写す
_FLAT_TO_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}


def _normalize_root(root: str) -> str:
    """根音を選択肢の表記（シャープ系）へ寄せる。読めなければ空文字。

    **小文字（"am" の "a"）も受ける。** 借りている部品の検証は大文字小文字を
    区別しないので、以前の自由入力欄では "am" のまま保存できた。
    フラット表記と同じく、黙って「指定しない」に倒すと設定が消える
    （開いて OK しただけでキーが空になる）。
    """
    root = root.strip()
    if root in KEY_ROOTS:
        return root
    cap = root.capitalize()          # "c#" -> "C#"、"db" -> "Db"
    if cap in KEY_ROOTS:
        return cap
    return _FLAT_TO_SHARP.get(cap, "")


def compose_key(root: str, minor: bool) -> str:
    """根音と長短から、設定に入れる文字列を作る（"A" / "Am"）。

    根音が空（指定しない）なら空文字。
    """
    if not root or root == KEY_NONE:
        return ""
    return f"{root}m" if minor else root


def split_key(key: str) -> tuple[str, bool]:
    """設定の文字列を (根音, 短調か) に分ける。

    読めない書式は「指定しない」に倒す（選択肢に無い値を選べないため）。
    `Am` `A` のほか、`Amin` `Amaj` のような書き方も受ける。
    フラット表記（`Db` など）は同じ音のシャープ表記の選択肢に写す。
    小文字（`am`）も選択肢の表記に寄せる。
    """
    text = (key or "").strip()
    if not text:
        return "", False

    # 長い接尾辞から順に見る（"m" は最後。"maj" を短調と誤らないため）
    lowered = text.lower()
    for suffix, minor in (("minor", True), ("major", False),
                          ("min", True), ("maj", False), ("m", True)):
        if lowered.endswith(suffix) and len(text) > len(suffix):
            root = text[:len(text) - len(suffix)].strip()
            return _normalize_root(root), minor
    return _normalize_root(text), False


def describe_key_ja(key: str) -> str:
    """キーの読み上げ文言。未設定なら「指定しない」。

    選択肢に無い書式は**そのまま返す**。値が入っているのに
    「指定しない」と読むと、設定が消えていると誤解されるため。
    """
    text = (key or "").strip()
    if not text:
        return KEY_NONE
    root, minor = split_key(text)
    if not root:
        return text
    return key_name(root, minor)


def key_name(root: str, minor: bool) -> str:
    """根音と長短の読み上げ（"Aマイナー"・"A minor"）。"""
    return tr(f"{root}{'マイナー' if minor else 'メジャー'}",
              f"{root} {'minor' if minor else 'major'}")


def parse_volume(value) -> float:
    """試聴の音量を読む。おかしな値は既定に戻す（0.0〜1.0 に収める）。

    **値は必ず丸めて正規化する。** 0.1 刻みの足し引きは浮動小数の誤差が
    積もる（0.2 + 0.1 = 0.30000000000000004）。そのままだと「端に着いた」
    の等値判定が実際のキー連打で成立せず、保存される値も汚れる。
    """
    try:
        volume = float(value)
    except (TypeError, ValueError):
        return DEFAULT_VOLUME
    if volume != volume:            # NaN
        return DEFAULT_VOLUME
    return round(max(0.0, min(1.0, volume)), 4)


def describe_volume_ja(volume: float) -> str:
    """音量の読み上げ文言。**割合で言う**（0.7 では伝わらない）。"""
    percent = int(round(max(0.0, min(1.0, volume)) * 100))
    if percent == 0:
        return tr("音量 0 パーセント。無音です", "Volume 0 percent. Silent")
    return tr(f"音量 {percent} パーセント", f"Volume {percent} percent")


def parse_bpm(value) -> tuple[float | None, str]:
    """入力されたテンポを読む。

    戻り値は (値, エラー文言)。空欄は「未設定」であってエラーではない。
    """
    if value is None:
        return None, ""
    text = _typed(value)
    if not text:
        return None, ""
    try:
        bpm = float(text)
    except ValueError:
        return None, tr(f"テンポは数値で入力してください（{text}）",
                        f"The tempo must be a number ({text})")
    if not (MIN_BPM <= bpm <= MAX_BPM):
        return None, tr(f"テンポは {MIN_BPM:g} から {MAX_BPM:g} "
                        f"の間で入力してください（{bpm:g}）",
                        f"The tempo must be between {MIN_BPM:g} and "
                        f"{MAX_BPM:g} ({bpm:g})")
    return bpm, ""


def parse_key(value) -> tuple[str, str]:
    """入力されたキーを読む。

    戻り値は (値, エラー文言)。空欄は「未設定」。
    書式の検証は借りている部品に任せる（無い環境では素通し）。
    """
    text = _typed(value)
    if not text:
        return "", ""
    try:
        from integration.render_service import validate_key

        error = validate_key(text)
    except ImportError:
        error = ""
    if error:
        return "", error
    return text, ""
