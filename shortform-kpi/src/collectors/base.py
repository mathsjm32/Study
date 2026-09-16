"""수집기 공통 유틸리티.

플랫폼별 수집기는 여기의 키 생성 규칙과 실행 래퍼를 공유한다.
"""
from __future__ import annotations

import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator, Sequence, TypeVar

from src import bq

T = TypeVar("T")

HASHTAG_RE = re.compile(r"#([^\s#,.!?()\[\]{}<>\"']+)")
ISO_DURATION_RE = re.compile(
    r"P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
)


# ---------------------------------------------------------------------------
# 키 생성 — 사람이 읽을 수 있는 형태로 만든다(디버깅과 조인이 쉬움)
# ---------------------------------------------------------------------------
def content_key(platform: str, content_id: str) -> str:
    return f"{platform}:{content_id}"


def account_key(platform: str, account_id: str) -> str:
    return f"{platform}:{account_id}"


def daily_key(content_key_: str, metric_date: str) -> str:
    return f"{content_key_}|{metric_date}"


def snapshot_key(content_key_: str, collected_at: datetime) -> str:
    """같은 시간대(시 단위)에 두 번 수집해도 한 행으로 합쳐지도록 버킷팅한다."""
    return f"{content_key_}|{collected_at:%Y-%m-%dT%H}"


def account_snapshot_key(account_key_: str, collected_at: datetime) -> str:
    return f"{account_key_}|{collected_at:%Y-%m-%dT%H}"


# ---------------------------------------------------------------------------
# 파싱 헬퍼
# ---------------------------------------------------------------------------
def parse_iso_duration(value: str | None) -> int | None:
    """ISO-8601 기간(PT1M30S)을 초로 바꾼다."""
    if not value:
        return None
    match = ISO_DURATION_RE.fullmatch(value)
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def extract_hashtags(*texts: str | None) -> list[str]:
    """제목·설명에서 해시태그를 뽑아 소문자로 정규화하고 중복을 제거한다."""
    found: list[str] = []
    for text in texts:
        for tag in HASHTAG_RE.findall(text or ""):
            tag = tag.lower()
            if tag not in found:
                found.append(tag)
    return found


def chunked(items: Sequence[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def as_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 실행 래퍼
# ---------------------------------------------------------------------------
@dataclass
class CollectResult:
    platform: str
    rows: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.rows.values())

    def record(self, table: str, count: int) -> None:
        self.rows[table] = self.rows.get(table, 0) + count

    def note(self, message: str) -> None:
        self.notes.append(message)
        print(f"    · {message}")


def run_collector(platform: str, fn: Callable[[CollectResult], None]) -> int:
    """수집기를 실행하고 결과를 ops_run_log 에 남긴다.

    실패해도 예외를 그대로 띄우지 않고 종료 코드로 알린다. 자동화에서
    한 플랫폼이 죽어도 나머지가 계속 돌아야 하기 때문이다.
    """
    started = datetime.now(timezone.utc)
    result = CollectResult(platform=platform)
    print(f"\n[{platform}] 수집 시작 — {started:%Y-%m-%d %H:%M:%S} UTC")
    print("-" * 60)

    try:
        fn(result)
    except Exception as exc:
        print(f"\n[{platform}] 실패: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        bq.log_run(platform, started, "failed", result.total, f"{type(exc).__name__}: {exc}")
        return 1

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print("-" * 60)
    for table, count in result.rows.items():
        print(f"  {table:<24} {count:>7,} 행")
    print(f"  {'합계':<24} {result.total:>7,} 행  ({elapsed:.1f}초)")

    bq.log_run(platform, started, "success", result.total, "; ".join(result.notes)[:1000])
    return 0
