"""Facebook Reels 수집기.

Instagram 과 마찬가지로 Page 영상 인사이트는 **누적값** 만 준다. 주기적으로
스냅샷을 찍고 일별 증분은 Step 4 의 SQL 뷰에서 차분으로 만든다.

Facebook 에는 '이 영상이 릴스인가'를 알려주는 단일 플래그가 없다. 두 신호를 쓴다.

  1) `permalink_url` 에 `/reel/` 이 들어 있으면 릴스다 (가장 확실)
  2) 길이가 `FB_REELS_MAX_SEC` 이하 (보조)

목록 수집도 이중화한다. `/{page-id}/video_reels` 엣지를 먼저 시도하고,
그 엣지를 쓸 수 없으면 `/{page-id}/videos` 로 내려가 위 신호로 릴스를 추린다.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from config.settings import (
    FB_METRIC_MAP,
    FB_MS_METRIC_MAP,
    FB_REELS_MAX_SEC,
    FB_REELS_METRICS,
    FB_SEC_METRIC_MAP,
    FB_VIDEO_FIELDS,
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

PLATFORM = "facebook"


# ---------------------------------------------------------------------------
# 목록
# ---------------------------------------------------------------------------
def resolve_video_fields(edge: str) -> list[str]:
    return meta.resolve_fields(edge, FB_VIDEO_FIELDS, label="Facebook 영상", limit=1)


def list_videos(result: CollectResult) -> tuple[list[dict], str]:
    """(영상 목록, 사용한 엣지). video_reels 를 먼저 시도한다."""
    for edge in (f"{META.fb_page_id}/video_reels", f"{META.fb_page_id}/videos"):
        try:
            fields = resolve_video_fields(edge)
            if not fields:
                continue
            videos = list(meta.paginate(edge, fields=",".join(fields), limit=50))
            print(f"  엣지 {edge.split('/')[-1]} 사용 — 영상 {len(videos)}개")
            return videos, edge
        except meta.MetaError as error:
            print(f"    [info] {edge.split('/')[-1]} 사용 불가: {error.message[:90]}")
    raise RuntimeError(
        "Facebook 영상 목록을 가져올 수 없습니다. 토큰에 pages_read_engagement 와 "
        "read_insights 권한이 있는지, 시스템 사용자에게 페이지 자산이 할당됐는지 확인하세요."
    )


def classify_reel(video: dict) -> tuple[bool, str]:
    """(릴스인가, 판정 근거)."""
    permalink = video.get("permalink_url") or ""
    if "/reel/" in permalink:
        return True, "포함: permalink 가 /reel/"
    length = as_float(video.get("length"))
    if length is None:
        return False, "제외: 릴스 신호 없음"
    if length <= FB_REELS_MAX_SEC:
        return True, f"포함: 길이 {FB_REELS_MAX_SEC}초 이하"
    return False, "제외: 길이 초과"


# ---------------------------------------------------------------------------
# 행 생성
# ---------------------------------------------------------------------------
def summary_count(video: dict, field: str) -> int | None:
    node = video.get(field) or {}
    return as_int((node.get("summary") or {}).get("total_count"))


def social_shares(values: dict[str, Any]) -> int | None:
    """post_video_social_actions 는 숫자 또는 액션별 분해 객체로 온다."""
    actions = values.get("post_video_social_actions")
    if isinstance(actions, dict):
        for key in ("share", "shares"):
            if key in actions:
                return as_int(actions[key])
    return None


def to_content_row(video: dict, now: dt.datetime) -> dict:
    created = video.get("created_time")
    description = video.get("description")
    return {
        "content_key": content_key(PLATFORM, video["id"]),
        "platform": PLATFORM,
        "content_id": video["id"],
        "account_id": META.fb_page_id,
        "published_at": created,
        "published_date": (created or "")[:10] or None,
        "duration_sec": as_int(video.get("length")),
        "aspect_ratio": None,  # Page 영상 API 는 해상도를 주지 않는다
        "title": video.get("title"),
        "caption": description,
        "hashtags": extract_hashtags(video.get("title"), description),
        "permalink": video.get("permalink_url"),
        "thumbnail_url": video.get("picture"),
        "media_format": "REELS",
        "first_seen_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "raw_json": json.dumps(video, ensure_ascii=False),
    }


def to_snapshot_row(video: dict, values: dict[str, Any], now: dt.datetime) -> dict:
    key = content_key(PLATFORM, video["id"])
    row: dict[str, Any] = {
        "snapshot_key": snapshot_key(key, now),
        "content_key": key,
        "platform": PLATFORM,
        "content_id": video["id"],
        "collected_at": now.isoformat(),
        "collected_date": now.date().isoformat(),
        "raw_json": json.dumps(values, ensure_ascii=False),
        "loaded_at": now.isoformat(),
    }

    row.update(meta.apply_map(values, FB_METRIC_MAP, convert=as_int))
    row.update(meta.apply_map(values, FB_MS_METRIC_MAP,
                              convert=lambda v: (as_float(v) or 0) / 1000))
    row.update(meta.apply_map(values, FB_SEC_METRIC_MAP, convert=as_float))

    # 좋아요·댓글은 인사이트가 아니라 영상 객체의 summary 에서 온다.
    row["likes"] = summary_count(video, "likes")
    row["comments"] = summary_count(video, "comments")
    row["shares"] = social_shares(values)
    row["saves"] = None  # Page 영상 인사이트에는 저장 지표가 없다

    row["total_interactions"] = sum(
        row.get(col) or 0 for col in ("likes", "comments", "shares", "saves")
    ) or None

    for column in ("views", "reach", "watch_time_sec", "avg_watch_sec"):
        row.setdefault(column, None)
    return row


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def collect(result: CollectResult, days: int | None = None) -> None:
    META.validate_facebook()
    now = dt.datetime.now(dt.timezone.utc)
    window = days if days is not None else INITIAL_BACKFILL_DAYS
    cutoff = now - dt.timedelta(days=window)

    # 1) 페이지
    page = meta.get(META.fb_page_id, fields="id,name,username,fan_count,followers_count")
    akey = account_key(PLATFORM, page["id"])
    followers = as_int(page.get("followers_count")) or as_int(page.get("fan_count"))
    print(f"  페이지: {page.get('name')} (팔로워 {followers or 0:,}명)")

    result.record("dim_account", bq.merge_rows("dim_account", [{
        "account_key": akey,
        "platform": PLATFORM,
        "account_id": page["id"],
        "name": page.get("name"),
        "handle": page.get("username"),
        "updated_at": now.isoformat(),
    }], key_cols=["account_key"]))

    result.record("fact_account_snapshot", bq.merge_rows("fact_account_snapshot", [{
        "account_snapshot_key": account_snapshot_key(akey, now),
        "account_key": akey,
        "platform": PLATFORM,
        "account_id": page["id"],
        "collected_at": now.isoformat(),
        "collected_date": now.date().isoformat(),
        "followers": followers,
        "media_count": None,
        "raw_json": json.dumps(page, ensure_ascii=False),
    }], key_cols=["account_snapshot_key"]))

    # 2) 영상 → 릴스 추리기
    videos, _ = list_videos(result)
    recent = [
        v for v in videos
        if not v.get("created_time")
        or dt.datetime.fromisoformat(v["created_time"]) >= cutoff
    ]

    decisions = [(v, *classify_reel(v)) for v in recent]
    reels = [v for v, ok, _ in decisions if ok]
    from collections import Counter
    for reason, count in sorted(Counter(r for _, _, r in decisions).items()):
        print(f"    {reason:<28} {count:>4}개")

    if recent and not reels:
        result.note(
            f"릴스 판별이 후보 {len(recent)}개를 모두 배제했습니다. "
            "dim_content 의 permalink 와 duration_sec 을 확인해 기준을 조정하세요."
        )
    if not reels:
        result.note("해당 기간에 릴스가 없습니다.")
        return
    print(f"  릴스 {len(reels)}개 (게시일 {cutoff.date()} 이후)")

    result.record("dim_content", bq.merge_rows(
        "dim_content", [to_content_row(v, now) for v in reels], key_cols=["content_key"]
    ))

    # 3) 지표 확정 (실행당 1회)
    metrics = meta.resolve_metrics(
        f"{reels[0]['id']}/video_insights", FB_REELS_METRICS, label="Facebook 릴스"
    )
    if not metrics:
        result.note("조회 가능한 영상 인사이트 지표가 없습니다. read_insights 권한을 확인하세요.")
        return
    print(f"  사용 지표 {len(metrics)}/{len(FB_REELS_METRICS)}개: {', '.join(metrics)}")
    if len(metrics) < len(FB_REELS_METRICS):
        dropped = [m for m in FB_REELS_METRICS if m not in metrics]
        result.note(f"제외된 지표: {', '.join(dropped)}")

    # 4) 릴스별 인사이트
    snapshots: list[dict] = []
    failed: list[str] = []
    for index, video in enumerate(reels, start=1):
        try:
            payload = meta.get(f"{video['id']}/video_insights", metric=",".join(metrics))
            values = meta.insight_values(payload)
        except meta.MetaError as error:
            failed.append(video["id"])
            if len(failed) <= 3:
                print(f"    [warn] {video['id']} 인사이트 실패: {error.message[:90]}")
            values = {}
        snapshots.append(to_snapshot_row(video, values, now))
        if index % 25 == 0:
            print(f"    진행 {index}/{len(reels)}")

    if failed:
        result.note(f"인사이트 조회 실패 {len(failed)}건 (예: {', '.join(failed[:3])})")

    result.record("fact_snapshot", bq.merge_rows(
        "fact_snapshot", snapshots, key_cols=["snapshot_key"]
    ))

    totals = {c: sum(s.get(c) or 0 for s in snapshots) for c in ("views", "reach", "shares")}
    print(f"  누적 합계 — 재생 {totals['views']:,} / 도달 {totals['reach']:,} "
          f"/ 공유 {totals['shares']:,}")
