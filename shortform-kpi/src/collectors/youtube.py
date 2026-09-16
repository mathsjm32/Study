"""YouTube Shorts 수집기.

두 API 를 함께 쓴다.

  * Data API v3      — 어떤 영상이 있고 길이·제목·게시일이 무엇인지 (메타데이터)
  * Analytics API v2 — 그 영상이 날짜별로 어떤 성과를 냈는지 (지표)

Shorts 판별은 길이로 한다. Data API 는 Shorts 여부를 알려주는 필드를 제공하지
않기 때문이다(`YT_SHORTS_MAX_SEC`).

일별 지표는 영상 하나씩 조회한다. Analytics API 의 `video` 차원 리포트는
기간 전체를 합산한 '상위 영상' 형태라 `day` 와 함께 쓸 수 없고, 날짜별
데이터는 `dimensions=day` + `filters=video==<id>` 로만 얻을 수 있다.
영상 수가 수백 개를 넘어가면 YouTube Reporting API(벌크 CSV)로 옮기는 편이 낫다.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import (
    ROLLING_REFETCH_DAYS,
    YT,
    YT_DAILY_METRICS,
    YT_METRIC_MAP,
    YT_SHORTS_MAX_SEC,
)
from src import bq
from src.collectors.base import (
    CollectResult,
    account_key,
    account_snapshot_key,
    as_float,
    as_int,
    chunked,
    content_key,
    daily_key,
    extract_hashtags,
    parse_iso_duration,
)

PLATFORM = "youtube"

# Analytics 데이터는 2~3일 지나야 확정된다. 그 전 구간은 매 실행마다 덮어쓴다.
ANALYTICS_LAG_DAYS = 1


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------
def build_services() -> tuple[Any, Any]:
    YT.validate()
    creds = Credentials(
        None,
        refresh_token=YT.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YT.client_id,
        client_secret=YT.client_secret,
    )
    creds.refresh(Request())
    data = build("youtube", "v3", credentials=creds, cache_discovery=False)
    analytics = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    return data, analytics


# ---------------------------------------------------------------------------
# Data API — 채널과 영상 메타데이터
# ---------------------------------------------------------------------------
def fetch_channel(data: Any) -> dict:
    response = data.channels().list(
        part="snippet,statistics,contentDetails", id=YT.channel_id
    ).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError(
            f"채널을 찾을 수 없습니다: {YT.channel_id}\n"
            "  .env 의 YT_CHANNEL_ID 가 맞는지 check_auth.py 로 확인하세요."
        )
    return items[0]


def fetch_video_ids(data: Any, uploads_playlist: str) -> list[str]:
    """업로드 재생목록을 순회해 모든 영상 ID 를 모은다.

    search.list(100 units) 대신 playlistItems.list(1 unit)를 쓴다. 쿼터를
    아껴야 매일 여러 번 돌릴 수 있다.
    """
    video_ids: list[str] = []
    page_token = None
    while True:
        response = data.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in response.get("items", []):
            video_id = item["contentDetails"].get("videoId")
            if video_id:
                video_ids.append(video_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            return video_ids


def fetch_video_details(data: Any, video_ids: list[str]) -> list[dict]:
    """영상 상세를 50개씩 묶어 가져온다(호출당 1 unit)."""
    details: list[dict] = []
    for batch in chunked(video_ids, 50):
        response = data.videos().list(
            part="snippet,contentDetails,statistics", id=",".join(batch)
        ).execute()
        details.extend(response.get("items", []))
    return details


def is_short(video: dict) -> bool:
    duration = parse_iso_duration(video.get("contentDetails", {}).get("duration"))
    return duration is not None and 0 < duration <= YT_SHORTS_MAX_SEC


def to_content_row(video: dict, now: dt.datetime) -> dict:
    snippet = video.get("snippet", {})
    published_at = snippet.get("publishedAt")
    thumbnails = snippet.get("thumbnails", {})
    thumb = thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}
    return {
        "content_key": content_key(PLATFORM, video["id"]),
        "platform": PLATFORM,
        "content_id": video["id"],
        "account_id": snippet.get("channelId"),
        "published_at": published_at,
        "published_date": (published_at or "")[:10] or None,
        "duration_sec": parse_iso_duration(video.get("contentDetails", {}).get("duration")),
        "title": snippet.get("title"),
        "caption": snippet.get("description"),
        "hashtags": extract_hashtags(snippet.get("title"), snippet.get("description")),
        "permalink": f"https://www.youtube.com/shorts/{video['id']}",
        "thumbnail_url": thumb.get("url"),
        "media_format": "SHORTS",
        "first_seen_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "raw_json": json.dumps(video, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# Analytics API — 영상별 일별 지표
# ---------------------------------------------------------------------------
def fetch_daily_rows(
    analytics: Any, video_id: str, start: dt.date, end: dt.date
) -> list[dict]:
    """한 영상의 날짜별 지표를 가져온다. 데이터가 없으면 빈 목록."""
    response = analytics.reports().query(
        ids=f"channel=={YT.channel_id}",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics=",".join(YT_DAILY_METRICS),
        dimensions="day",
        filters=f"video=={video_id}",
    ).execute()

    headers = [h["name"] for h in response.get("columnHeaders", [])]
    rows = []
    for raw in response.get("rows", []):
        record = dict(zip(headers, raw))
        rows.append(build_daily_row(video_id, record))
    return rows


def build_daily_row(video_id: str, record: dict) -> dict:
    key = content_key(PLATFORM, video_id)
    metric_date = record["day"]

    row: dict[str, Any] = {
        "daily_key": daily_key(key, metric_date),
        "content_key": key,
        "platform": PLATFORM,
        "content_id": video_id,
        "metric_date": metric_date,
        "reach": None,  # YouTube 는 도달(고유 시청자)을 제공하지 않는다
        "source": "api",
        "loaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    for api_name, column in YT_METRIC_MAP.items():
        value = record.get(api_name)
        row[column] = as_float(value) if column in {"avg_watch_sec", "avg_view_pct"} else as_int(value)

    # 분 단위로 오는 시청 시간을 초로 통일한다(다른 플랫폼과 맞추기 위해).
    minutes = as_float(record.get("estimatedMinutesWatched"))
    row["watch_time_sec"] = minutes * 60 if minutes is not None else None

    gained = as_int(record.get("subscribersGained")) or 0
    lost = as_int(record.get("subscribersLost")) or 0
    row["followers_gained"] = gained - lost

    row["total_interactions"] = sum(
        row.get(col) or 0 for col in ("likes", "comments", "shares", "saves")
    )
    return row


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def collect(result: CollectResult, days: int | None = None) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    end = now.date() - dt.timedelta(days=ANALYTICS_LAG_DAYS)
    window = days if days is not None else ROLLING_REFETCH_DAYS
    start = end - dt.timedelta(days=window - 1)
    print(f"  수집 기간: {start} ~ {end} ({window}일)")

    data, analytics = build_services()

    # 1) 채널 정보
    channel = fetch_channel(data)
    stats = channel.get("statistics", {})
    akey = account_key(PLATFORM, channel["id"])
    result.record("dim_account", bq.merge_rows("dim_account", [{
        "account_key": akey,
        "platform": PLATFORM,
        "account_id": channel["id"],
        "name": channel["snippet"]["title"],
        "handle": channel["snippet"].get("customUrl"),
        "updated_at": now.isoformat(),
    }], key_cols=["account_key"]))

    result.record("fact_account_snapshot", bq.merge_rows("fact_account_snapshot", [{
        "account_snapshot_key": account_snapshot_key(akey, now),
        "account_key": akey,
        "platform": PLATFORM,
        "account_id": channel["id"],
        "collected_at": now.isoformat(),
        "collected_date": now.date().isoformat(),
        "followers": as_int(stats.get("subscriberCount")),
        "media_count": as_int(stats.get("videoCount")),
        "raw_json": json.dumps(stats, ensure_ascii=False),
    }], key_cols=["account_snapshot_key"]))

    print(f"  채널: {channel['snippet']['title']} "
          f"(구독자 {as_int(stats.get('subscriberCount')) or 0:,}명)")

    # 2) 영상 목록에서 Shorts 만 추린다
    uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    video_ids = fetch_video_ids(data, uploads)
    videos = fetch_video_details(data, video_ids)
    shorts = [v for v in videos if is_short(v)]
    print(f"  영상 {len(videos)}개 중 Shorts {len(shorts)}개 "
          f"(기준: {YT_SHORTS_MAX_SEC}초 이하)")

    if not shorts:
        result.note("Shorts 가 없습니다. 채널에 숏폼 업로드가 있는지 확인하세요.")
        return

    result.record("dim_content", bq.merge_rows(
        "dim_content", [to_content_row(v, now) for v in shorts],
        key_cols=["content_key"],
    ))

    # 3) Shorts 각각의 일별 지표
    daily_rows: list[dict] = []
    failed: list[str] = []
    for index, video in enumerate(shorts, start=1):
        try:
            daily_rows.extend(fetch_daily_rows(analytics, video["id"], start, end))
        except HttpError as exc:
            failed.append(video["id"])
            if len(failed) <= 3:  # 같은 오류를 수백 줄 찍지 않는다
                print(f"    [warn] {video['id']} 지표 조회 실패: {exc.reason}")
        if index % 25 == 0:
            print(f"    진행 {index}/{len(shorts)}")

    if failed:
        result.note(f"지표 조회 실패 {len(failed)}건 (예: {', '.join(failed[:3])})")

    result.record("fact_daily", bq.merge_rows(
        "fact_daily", daily_rows, key_cols=["daily_key"]
    ))

    if daily_rows:
        total_views = sum(r.get("views") or 0 for r in daily_rows)
        total_shares = sum(r.get("shares") or 0 for r in daily_rows)
        total_saves = sum(r.get("saves") or 0 for r in daily_rows)
        print(f"  기간 합계 — 조회 {total_views:,} / 공유 {total_shares:,} / 저장 {total_saves:,}")
    else:
        result.note("해당 기간에 지표 데이터가 없습니다(최근 업로드가 없거나 기간이 너무 짧음).")
