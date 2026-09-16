"""Instagram Reels 수집기.

Instagram 미디어 인사이트는 **누적값(lifetime)** 만 준다. 날짜별 데이터가
없으므로 주기적으로 누적값을 찍어 `fact_snapshot` 에 쌓고, 일별 증분은
Step 4 의 SQL 뷰에서 스냅샷 차분으로 만든다.

지표 이름은 고정하지 않는다. Meta 가 자주 바꾸기 때문에, 실행 시작 시
`resolve_metrics` 로 이 계정에서 실제로 되는 지표를 확정하고 그 목록만 쓴다.
원본 응답은 `raw_json` 에 그대로 남겨 나중에 재해석할 수 있게 한다.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from config.settings import (
    IG_MEDIA_METRICS,
    IG_METRIC_MAP,
    IG_MS_METRIC_MAP,
    INITIAL_BACKFILL_DAYS,
    META,
)
from src import bq, meta
from src.collectors.base import (
    CollectResult,
    account_key,
    account_snapshot_key,
    as_float,
    as_int,
    content_key,
    extract_hashtags,
    snapshot_key,
)

PLATFORM = "instagram"

MEDIA_FIELDS = ",".join([
    "id", "caption", "media_type", "media_product_type", "permalink",
    "thumbnail_url", "media_url", "timestamp", "like_count", "comments_count",
])


def fetch_account(result: CollectResult) -> dict:
    return meta.get(
        META.ig_user_id, fields="id,username,name,followers_count,media_count"
    )


def fetch_reels(cutoff: dt.datetime, result: CollectResult) -> list[dict]:
    """게시일이 cutoff 이후인 릴스를 모은다.

    미디어 목록은 최신순이므로 cutoff 보다 오래된 항목이 나오면 멈춘다.
    """
    reels: list[dict] = []
    scanned = 0
    for media in meta.paginate(f"{META.ig_user_id}/media", fields=MEDIA_FIELDS):
        scanned += 1
        timestamp = media.get("timestamp")
        if timestamp and dt.datetime.fromisoformat(timestamp) < cutoff:
            break
        if media.get("media_product_type") == "REELS":
            reels.append(media)
    print(f"  미디어 {scanned}개 확인 → 릴스 {len(reels)}개 (게시일 {cutoff.date()} 이후)")
    return reels


def to_content_row(media: dict, now: dt.datetime) -> dict:
    caption = media.get("caption")
    timestamp = media.get("timestamp")
    return {
        "content_key": content_key(PLATFORM, media["id"]),
        "platform": PLATFORM,
        "content_id": media["id"],
        "account_id": META.ig_user_id,
        "published_at": timestamp,
        "published_date": (timestamp or "")[:10] or None,
        "duration_sec": None,  # IG 미디어 API 는 영상 길이를 주지 않는다
        "aspect_ratio": None,
        "title": None,  # 릴스는 제목 개념이 없다. 캡션만 있다.
        "caption": caption,
        "hashtags": extract_hashtags(caption),
        "permalink": media.get("permalink"),
        "thumbnail_url": media.get("thumbnail_url") or media.get("media_url"),
        "media_format": "REELS",
        "first_seen_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "raw_json": json.dumps(media, ensure_ascii=False),
    }


def to_snapshot_row(media: dict, values: dict[str, Any], now: dt.datetime) -> dict:
    key = content_key(PLATFORM, media["id"])
    row: dict[str, Any] = {
        "snapshot_key": snapshot_key(key, now),
        "content_key": key,
        "platform": PLATFORM,
        "content_id": media["id"],
        "collected_at": now.isoformat(),
        "collected_date": now.date().isoformat(),
        "raw_json": json.dumps(values, ensure_ascii=False),
        "loaded_at": now.isoformat(),
    }

    for metric, column in IG_METRIC_MAP.items():
        row[column] = as_int(values.get(metric))
    for metric, column in IG_MS_METRIC_MAP.items():
        millis = as_float(values.get(metric))
        row[column] = millis / 1000 if millis is not None else None

    # 인사이트가 빠진 지표는 미디어 필드 값으로 메운다.
    if row.get("likes") is None:
        row["likes"] = as_int(media.get("like_count"))
    if row.get("comments") is None:
        row["comments"] = as_int(media.get("comments_count"))
    if row.get("total_interactions") is None:
        row["total_interactions"] = sum(
            row.get(col) or 0 for col in ("likes", "comments", "shares", "saves")
        ) or None
    return row


def collect(result: CollectResult, days: int | None = None) -> None:
    META.validate_instagram()
    now = dt.datetime.now(dt.timezone.utc)
    window = days if days is not None else INITIAL_BACKFILL_DAYS
    cutoff = now - dt.timedelta(days=window)

    # 1) 계정
    account = fetch_account(result)
    akey = account_key(PLATFORM, account["id"])
    print(f"  계정: @{account.get('username')} "
          f"(팔로워 {as_int(account.get('followers_count')) or 0:,}명)")

    result.record("dim_account", bq.merge_rows("dim_account", [{
        "account_key": akey,
        "platform": PLATFORM,
        "account_id": account["id"],
        "name": account.get("name"),
        "handle": account.get("username"),
        "updated_at": now.isoformat(),
    }], key_cols=["account_key"]))

    result.record("fact_account_snapshot", bq.merge_rows("fact_account_snapshot", [{
        "account_snapshot_key": account_snapshot_key(akey, now),
        "account_key": akey,
        "platform": PLATFORM,
        "account_id": account["id"],
        "collected_at": now.isoformat(),
        "collected_date": now.date().isoformat(),
        "followers": as_int(account.get("followers_count")),
        "media_count": as_int(account.get("media_count")),
        "raw_json": json.dumps(account, ensure_ascii=False),
    }], key_cols=["account_snapshot_key"]))

    # 2) 릴스 목록
    reels = fetch_reels(cutoff, result)
    if not reels:
        result.note("해당 기간에 릴스가 없습니다.")
        return

    result.record("dim_content", bq.merge_rows(
        "dim_content", [to_content_row(m, now) for m in reels], key_cols=["content_key"]
    ))

    # 3) 이 계정에서 실제로 되는 지표를 한 번만 확정한다
    metrics = meta.resolve_metrics(
        f"{reels[0]['id']}/insights", IG_MEDIA_METRICS, label="Instagram 릴스"
    )
    if not metrics:
        result.note("조회 가능한 인사이트 지표가 없습니다. 토큰 권한을 확인하세요.")
        return
    print(f"  사용 지표 {len(metrics)}/{len(IG_MEDIA_METRICS)}개: {', '.join(metrics)}")
    if len(metrics) < len(IG_MEDIA_METRICS):
        dropped = [m for m in IG_MEDIA_METRICS if m not in metrics]
        result.note(f"제외된 지표: {', '.join(dropped)}")

    # 4) 릴스별 인사이트 수집
    snapshots: list[dict] = []
    failed: list[str] = []
    for index, media in enumerate(reels, start=1):
        try:
            payload = meta.get(f"{media['id']}/insights", metric=",".join(metrics))
            values = meta.insight_values(payload)
        except meta.MetaError as error:
            failed.append(media["id"])
            if len(failed) <= 3:
                print(f"    [warn] {media['id']} 인사이트 실패: {error.message[:90]}")
            values = {}
        snapshots.append(to_snapshot_row(media, values, now))
        if index % 25 == 0:
            print(f"    진행 {index}/{len(reels)}")

    if failed:
        result.note(f"인사이트 조회 실패 {len(failed)}건 (예: {', '.join(failed[:3])})")

    result.record("fact_snapshot", bq.merge_rows(
        "fact_snapshot", snapshots, key_cols=["snapshot_key"]
    ))

    totals = {c: sum(s.get(c) or 0 for s in snapshots)
              for c in ("views", "reach", "saves", "shares")}
    print(f"  누적 합계 — 조회 {totals['views']:,} / 도달 {totals['reach']:,} "
          f"/ 저장 {totals['saves']:,} / 공유 {totals['shares']:,}")
