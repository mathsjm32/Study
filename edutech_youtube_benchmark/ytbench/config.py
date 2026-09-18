"""경로·환경변수·분석 상수 정의."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path

# ── 경로 ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
CACHE = DATA / "cache"
OUTPUT = ROOT / "output"

# 입력
COMPANIES_CSV = DATA / "companies.csv"

# 중간 산출물
RESOLVED_CSV = PROCESSED / "channels_resolved.csv"
CHANNELS_CSV = PROCESSED / "channels_raw.csv"
VIDEOS_CSV = PROCESSED / "videos_raw.csv"
VIDEO_FEATURES_CSV = PROCESSED / "video_features.csv"
CHANNEL_METRICS_CSV = PROCESSED / "channel_metrics.csv"
SCORES_CSV = PROCESSED / "channel_scores.csv"
INSIGHTS_JSON = PROCESSED / "llm_insights.json"

# 최종 산출물
REPORT_XLSX = OUTPUT / "에듀테크_경쟁사_유튜브_벤치마킹.xlsx"

QUOTA_STATE = CACHE / "quota_state.json"
SEARCH_CACHE = CACHE / "search"          # 회사명별 search.list 응답 캐시

KST = timezone(timedelta(hours=9))

# ── 환경변수 ──────────────────────────────────────────────────────────
ENV_YOUTUBE_KEY = "YOUTUBE_API_KEY"
ENV_ANTHROPIC_KEY = "ANTHROPIC_API_KEY"


def youtube_api_key() -> str | None:
    return os.environ.get(ENV_YOUTUBE_KEY) or None


def has_anthropic_key() -> bool:
    return bool(os.environ.get(ENV_ANTHROPIC_KEY))


# ── 쿼터 ──────────────────────────────────────────────────────────────
# YouTube Data API v3 무료 기본 할당량: 10,000 units/일 (태평양시 자정 리셋)
DAILY_QUOTA = 10_000
QUOTA_COST = {
    "search.list": 100,      # 비싸다 -> 회사명 검색에만, 캐시 필수
    "channels.list": 1,
    "playlistItems.list": 1,
    "videos.list": 1,
    "playlists.list": 1,
}

# ── 수집 파라미터 ─────────────────────────────────────────────────────
@dataclass
class CollectConfig:
    """수집 범위. 넓히면 품질이 오르지만 쿼터/시간이 늘어난다."""

    max_videos_per_channel: int = 100   # 채널당 최근 N개 영상
    lookback_days: int = 365            # N일 이내 업로드만 (0이면 제한 없음)
    search_candidates: int = 5          # 회사명 1건당 검색 후보 수
    region_code: str = "KR"
    relevance_language: str = "ko"
    request_sleep: float = 0.05         # API 호출 간 대기(초)
    max_retries: int = 4                # 5xx/네트워크 오류 재시도 횟수


# ── 분석 상수 ─────────────────────────────────────────────────────────
SHORTS_MAX_SECONDS = 60          # 이하 = 숏폼
MID_MAX_SECONDS = 600            # 60초 초과 ~ 10분 = 미들폼, 초과 = 롱폼
HIT_MULTIPLIER = 3.0             # 채널 중위 조회수의 N배 이상 = 히트 영상
RECENT_WINDOW_DAYS = 90          # 최근 활동 판정 창
MOMENTUM_WINDOW_DAYS = 90        # 모멘텀 비교 창 (최근 90일 vs 직전 90일)

# 제목 패턴 탐지 정규식 (한국어 유튜브 관행 기준)
TITLE_PATTERNS: dict[str, str] = {
    "숫자포함": r"\d",
    "대괄호태그": r"[\[\(].{1,20}[\]\)]",
    "물음표": r"\?|？",
    "느낌표": r"!|！",
    "이모지": r"[\U0001F300-\U0001FAFF☀-➿]",
    "말줄임_후킹": r"\.\.\.|…",
    "따옴표강조": r"['\"'\"「」]",
    "숏폼해시태그": r"#[Ss]horts|#쇼츠",
}

# 에듀테크 문맥에서 자주 쓰이는 후킹 키워드 (제목 전략 분석용)
HOOK_KEYWORDS: dict[str, list[str]] = {
    "성과_인증": ["합격", "성적", "등급", "점수", "후기", "만점", "역전", "수석"],
    "공포_긴급": ["주의", "실수", "망하", "절대", "위험", "마감", "지금", "늦기"],
    "정보_가이드": ["방법", "가이드", "총정리", "정리", "꿀팁", "노하우", "전략", "분석"],
    "권위_전문가": ["강사", "전문가", "선생님", "교수", "멘토", "1위", "대표"],
    "신규_트렌드": ["신규", "오픈", "출시", "최신", "개정", "2025", "2026", "AI"],
    "참여_유도": ["구독", "댓글", "알림", "이벤트", "무료", "신청", "링크"],
}

# 벤치마크 스코어 6축 가중치 (합 1.0)
SCORE_WEIGHTS: dict[str, float] = {
    "도달력": 0.15,       # 구독자·총조회수 규모
    "효율성": 0.25,       # 구독자당 조회수, 영상당 중위 조회수
    "참여도": 0.20,       # 좋아요+댓글 / 조회수
    "일관성": 0.15,       # 업로드 빈도 + 규칙성
    "콘텐츠설계": 0.15,   # 제목·길이·포맷 최적화 정도
    "최신성": 0.10,       # 최근 업로드 활성도
}


@dataclass
class LLMConfig:
    """LLM 정성 분석 설정."""

    model: str = "claude-opus-5"
    max_tokens: int = 8000
    effort: str = "medium"          # low | medium | high | xhigh | max
    top_n_videos: int = 8           # 채널별 상위 영상 표본
    bottom_n_videos: int = 4        # 채널별 하위 영상 표본
    analyze_thumbnails: bool = False  # True면 썸네일 이미지까지 Vision 분석
    thumbnail_sample: int = 3       # 썸네일 분석 시 채널별 이미지 수
    use_batch_api: bool = True      # Batch API 사용(50% 비용, 최대 24h)
    max_channels: int | None = None  # 비용 제한용 상한 (None = 전체)


@dataclass
class Settings:
    collect: CollectConfig = field(default_factory=CollectConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)


def ensure_dirs() -> None:
    for d in (DATA, RAW, PROCESSED, CACHE, SEARCH_CACHE, OUTPUT):
        d.mkdir(parents=True, exist_ok=True)
