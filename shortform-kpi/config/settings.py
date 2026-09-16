"""환경 변수 로딩과 전역 설정.

.env 파일(로컬) 또는 OS 환경 변수(GitHub Actions)에서 설정을 읽는다.
값이 비어 있어도 import 자체는 실패하지 않는다 — 필요한 시점에
`require()` 로 검증한다. (수집기 하나만 돌릴 때 다른 플랫폼 자격증명이
없어도 동작해야 하기 때문)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

PLATFORMS = ("youtube", "instagram", "facebook")


class ConfigError(RuntimeError):
    """필수 환경 변수가 비어 있을 때."""


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def require(**values: str) -> None:
    """빈 값이 있으면 어떤 환경 변수가 비었는지 알려주며 중단한다."""
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(
            f".env 에 다음 값이 비어 있습니다: {', '.join(missing)}\n"
            f"  → {ROOT / '.env.example'} 를 참고해 채워 주세요."
        )


@dataclass(frozen=True)
class BigQueryConfig:
    project_id: str = _env("GCP_PROJECT_ID")
    dataset: str = _env("BQ_DATASET", "shortform_kpi")
    location: str = _env("BQ_LOCATION", "asia-northeast3")
    credentials_path: str = _env("GOOGLE_APPLICATION_CREDENTIALS")
    credentials_json: str = _env("GCP_SA_JSON")

    @property
    def dataset_ref(self) -> str:
        return f"{self.project_id}.{self.dataset}"

    def table(self, name: str) -> str:
        return f"{self.project_id}.{self.dataset}.{name}"

    def validate(self) -> None:
        require(GCP_PROJECT_ID=self.project_id, BQ_DATASET=self.dataset)


@dataclass(frozen=True)
class YouTubeConfig:
    client_id: str = _env("YT_CLIENT_ID")
    client_secret: str = _env("YT_CLIENT_SECRET")
    refresh_token: str = _env("YT_REFRESH_TOKEN")
    channel_id: str = _env("YT_CHANNEL_ID")

    def validate(self) -> None:
        require(
            YT_CLIENT_ID=self.client_id,
            YT_CLIENT_SECRET=self.client_secret,
            YT_REFRESH_TOKEN=self.refresh_token,
            YT_CHANNEL_ID=self.channel_id,
        )


@dataclass(frozen=True)
class MetaConfig:
    api_version: str = _env("META_API_VERSION", "v26.0")
    access_token: str = _env("META_ACCESS_TOKEN")
    ig_user_id: str = _env("IG_USER_ID")
    fb_page_id: str = _env("FB_PAGE_ID")

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self.api_version}"

    def validate_instagram(self) -> None:
        require(META_ACCESS_TOKEN=self.access_token, IG_USER_ID=self.ig_user_id)

    def validate_facebook(self) -> None:
        require(META_ACCESS_TOKEN=self.access_token, FB_PAGE_ID=self.fb_page_id)


BQ = BigQueryConfig()
YT = YouTubeConfig()
META = MetaConfig()

SLACK_WEBHOOK_URL = _env("SLACK_WEBHOOK_URL")

# ---------------------------------------------------------------------------
# 수집 지표 후보 목록
#
# Meta 는 2025~2026 년에 걸쳐 impressions → views 통합, reach → viewer 계열
# 교체를 진행 중이라 지표 이름이 버전마다 달라진다. 그래서 '요청해 볼 후보'
# 로만 두고, API 가 특정 지표를 거부하면 그 지표만 빼고 재시도한다
# (src/collectors/base.py 의 degrade-and-retry 로직). 실제로 어떤 지표가
# 살아 있는지는 Step 2/3 에서 사용자의 실제 토큰으로 검증한다.
# ---------------------------------------------------------------------------

IG_MEDIA_METRICS = [
    "views",
    "reach",
    "likes",
    "comments",
    "shares",
    "saved",
    "total_interactions",
    "ig_reels_avg_watch_time",
    "ig_reels_video_view_total_time",
]

FB_REELS_METRICS = [
    "blue_reels_play_count",
    "post_impressions_unique",
    "post_video_avg_time_watched",
    "post_video_view_time",
    "post_video_social_actions",
]

# YouTube Analytics API 는 이름이 안정적이라 고정으로 둔다.
YT_DAILY_METRICS = [
    "views",
    "likes",
    "comments",
    "shares",
    "videosAddedToPlaylists",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
    "subscribersLost",
]

# YouTube Analytics 수치는 2~3일간 확정되지 않는다. 매 실행마다 최근 N일을
# 다시 가져와 덮어쓴다(rolling re-fetch).
ROLLING_REFETCH_DAYS = 3

# 첫 실행 시 소급 수집할 기간.
INITIAL_BACKFILL_DAYS = 90

# YouTube 는 API 에 '이 영상이 Shorts 인가' 플래그를 주지 않는다. 두 신호를 함께 쓴다.
#
#   1) 길이   — Shorts 상한은 현재 3분. 이걸 넘으면 Shorts 가 아니다(필요조건).
#   2) 종횡비 — videos.list 에 part=player 와 maxHeight 를 주면 embedWidth/embedHeight
#              가 영상의 실제 비율로 돌아온다. 9:16 이면 0.5625, 16:9 면 1.78.
#
# 길이만으로는 30초짜리 가로 영상이 Shorts 로 잘못 분류된다. 비율을 함께 보면
# 그 오분류가 사라진다. 비율 정보를 못 얻은 영상은 길이만으로 판정한다.
YT_SHORTS_MAX_SEC = 180
YT_SHORTS_MAX_ASPECT = 1.0  # 세로 또는 정사각(width/height <= 1)만 Shorts 로 본다
YT_PLAYER_PROBE_HEIGHT = 720  # 비율 계산용. 값 자체는 의미 없고 비율만 쓴다

# Analytics 지표를 통합 스키마 컬럼으로 옮기는 규칙.
# videosAddedToPlaylists 는 '저장'과 완전히 같지는 않지만 가장 가까운 대용이다.
YT_METRIC_MAP = {
    "views": "views",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "videosAddedToPlaylists": "saves",
    "averageViewDuration": "avg_watch_sec",
    "averageViewPercentage": "avg_view_pct",
}
