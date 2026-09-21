#!/usr/bin/env python3
"""OMNIA イベント台帳（events.md）のパーサ・検証・取り込み。

events.md は別リポジトリ（omnia-events）で Claude のクラウドルーチンが毎朝更新し、
OMNIA 側はこのスクリプトで検証してから取り込む。**取り込む内容は LLM が Web から
集めたものなので信用しない。** 書式に合わないものは1行でも混ざっていたら丸ごと拒否する。

使い方:
    python scripts/events.py check [path]   書式を検証する（既定: events.md）
    python scripts/events.py sync           EVENTS_SOURCE_URL から取得→検証→events.md を置換

環境変数:
    EVENTS_SOURCE_URL  取り込み元の raw URL。既定は omnia-events の main。
    OMNIA_ROOT         任意。既定はこのファイルの1つ上のディレクトリ。

終了コード:
    0 正常（sync で変更なしも含む） / 1 検証エラー・取得失敗（events.md は書き換えない）
"""

from __future__ import annotations

import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

DEFAULT_SOURCE_URL = "https://raw.githubusercontent.com/tbou30897/omnia-events/main/events.md"

KINDS = ("doujin", "coffee")
MAX_BYTES = 64 * 1024
MAX_EVENTS = 200
MAX_NAME = 80
MAX_FIELD = 80

LINE_RE = re.compile(r"^-\s*\[(?P<kind>[a-z]+)\]\s*(?P<rest>.+)$")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.\.(\d{4}-\d{2}-\d{2}))?$")
URL_RE = re.compile(r"^https?://[^\s<>\"'|]+$")
TAG_RE = re.compile(r"^#[^\s#]+$")
UPDATED_RE = re.compile(r"^updated:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
# Discord のメンションや HTML/Markdown リンクとして解釈されうる文字列は入れさせない
FORBIDDEN_RE = re.compile(r"@everyone|@here|<@|<#|[<>\[\]`]|\]\(")


@dataclass
class Event:
    kind: str
    start: date
    end: date
    name: str
    venue: str
    pref: str
    deadline: date | None
    tags: list[str]
    url: str
    note: str


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"日付が不正: {s}") from None


def parse_event_line(line: str) -> Event:
    """1行をパースする。書式違反は ValueError。"""
    m = LINE_RE.match(line.strip())
    if not m:
        raise ValueError("`- [kind] ...` の形になっていない")
    kind = m.group("kind")
    if kind not in KINDS:
        raise ValueError(f"kind が不正: {kind}")

    cols = [c.strip() for c in m.group("rest").split("|")]
    if len(cols) not in (7, 8):
        raise ValueError(f"列数が不正（{len(cols)}列。7〜8列が必要）")
    when, name, venue, pref, deadline_s, tags_s, url = cols[:7]
    note = cols[7] if len(cols) == 8 else ""

    dm = DATE_RE.match(when)
    if not dm:
        raise ValueError(f"開催日が不正: {when}")
    start = _parse_date(dm.group(1))
    end = _parse_date(dm.group(2)) if dm.group(2) else start
    if end < start:
        raise ValueError("開催期間の終了日が開始日より前")

    deadline = None if deadline_s == "-" else _parse_date(deadline_s)

    if not name or len(name) > MAX_NAME:
        raise ValueError("名称が空、または長すぎる")
    for label, v in (("会場", venue), ("都県", pref), ("備考", note)):
        if len(v) > MAX_FIELD:
            raise ValueError(f"{label}が長すぎる")
    if not venue or not pref:
        raise ValueError("会場・都県は空にしない（不明なら `-`）")

    tags = tags_s.split()
    if not tags or not all(TAG_RE.match(t) for t in tags):
        raise ValueError(f"タグが不正: {tags_s}")
    if not URL_RE.match(url):
        raise ValueError(f"URL が不正: {url}")

    for v in (name, venue, pref, note, tags_s):
        if FORBIDDEN_RE.search(v):
            raise ValueError(f"使用できない文字列を含む: {v}")

    return Event(kind, start, end, name, venue, pref, deadline, tags, url, note)


def parse_events(text: str) -> tuple[list[Event], list[str]]:
    """(イベント一覧, エラー一覧) を返す。`- [` で始まる行はすべてイベント行として扱う。"""
    events: list[Event] = []
    errors: list[str] = []
    in_fence = False
    for no, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        # コードブロック内は書式説明の例なので無視する
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not s.startswith("- ["):
            continue
        try:
            events.append(parse_event_line(s))
        except ValueError as e:
            errors.append(f"L{no}: {e}")
    return events, errors


def updated_date(text: str) -> date | None:
    m = UPDATED_RE.search(text)
    if not m:
        return None
    try:
        return _parse_date(m.group(1))
    except ValueError:
        return None


def validate(text: str) -> list[str]:
    """ファイル全体を検証し、エラー一覧を返す（空なら合格）。"""
    errors: list[str] = []
    if len(text.encode("utf-8")) > MAX_BYTES:
        errors.append(f"ファイルが大きすぎる（上限 {MAX_BYTES} bytes）")
    if updated_date(text) is None:
        errors.append("frontmatter に `updated: YYYY-MM-DD` が無い")
    events, line_errors = parse_events(text)
    errors.extend(line_errors)
    if len(events) > MAX_EVENTS:
        errors.append(f"件数が多すぎる（{len(events)}件。上限 {MAX_EVENTS}）")
    return errors


def load_events(path: Path) -> tuple[list[Event], date | None]:
    """表示用。壊れた行は黙って捨てる（取り込み時に検証済みの前提）。"""
    if not path.exists():
        return [], None
    text = path.read_text(encoding="utf-8")
    events, _ = parse_events(text)
    return events, updated_date(text)


def cmd_check(path: Path) -> int:
    if not path.exists():
        print(f"error: not found: {path}", file=sys.stderr)
        return 1
    text = path.read_text(encoding="utf-8")
    errors = validate(text)
    if errors:
        print(f"NG: {path}", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    events, _ = parse_events(text)
    print(f"OK: {path}（{len(events)}件 / updated {updated_date(text)}）")
    return 0


def cmd_sync(root: Path) -> int:
    url = os.environ.get("EVENTS_SOURCE_URL", "").strip() or DEFAULT_SOURCE_URL
    if not url.startswith("https://"):
        print("error: EVENTS_SOURCE_URL は https:// のみ", file=sys.stderr)
        return 1
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "omnia-events-sync/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(MAX_BYTES + 1)
    except Exception as e:  # noqa: BLE001
        print(f"error: 取得失敗: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        print("error: UTF-8 として読めない", file=sys.stderr)
        return 1

    if errors := validate(text):
        # 壊れた内容で上書きしない。既存の events.md を残し、古さは remind.py が警告する
        print("error: 取り込み元が検証に通らないため events.md は更新しない", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    text = text.replace("\r\n", "\n")
    dest = root / "events.md"
    if dest.exists() and dest.read_text(encoding="utf-8") == text:
        print("events.md: 変更なし")
        return 0
    dest.write_text(text, encoding="utf-8", newline="\n")
    events, _ = parse_events(text)
    print(f"events.md: 更新（{len(events)}件 / updated {updated_date(text)}）")
    return 0


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    root = Path(os.environ.get("OMNIA_ROOT") or Path(__file__).resolve().parent.parent)
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "check":
        return cmd_check(Path(argv[2]) if len(argv) > 2 else root / "events.md")
    if cmd == "sync":
        return cmd_sync(root)
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
