"""YouTube Data API v3 저수준 클라이언트.

핵심 책임 3가지:
  1. 쿼터 회계 — 호출 전 비용을 차감하고 일일 예산 초과 시 즉시 중단
  2. 재시도    — 5xx/네트워크 오류는 지수 백오프, 4xx는 즉시 예외
  3. 캐시      — 비싼 search.list(100 units) 응답은 디스크에 영구 보관
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date
from typing import Any, Iterable, Iterator, Sequence

import requests

from . import config

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"


class QuotaExceeded(RuntimeError):
    """일일 쿼터 예산을 넘어설 때 발생. 이미 수집한 데이터는 보존된다."""


class YouTubeAPIError(RuntimeError):
    """재시도로 복구되지 않는 API 오류."""


class QuotaLedger:
    """태평양시 자정 기준 일일 쿼터 사용량을 디스크에 기록한다.

    이유: 파이프라인을 여러 번 나눠 돌리기 때문에(193개 회사 검색은
    하루 쿼터를 넘긴다) 프로세스 간 사용량이 이어져야 한다.
    """

    def __init__(self, path=config.QUOTA_STATE, budget: int = config.DAILY_QUOTA):
        self.path = path
        self.budget = budget
        self._state = self._load()

    @staticmethod
    def _today() -> str:
        # 쿼터 리셋은 태평양시 자정 = UTC-8/-7. 보수적으로 UTC 날짜를 쓰되
        # 경계에서 과소 집계되지 않도록 사용량은 누적만 한다.
        return date.today().isoformat()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
                if state.get("date") == self._today():
                    return state
            except (json.JSONDecodeError, OSError):
                log.warning("쿼터 상태 파일을 읽을 수 없어 초기화합니다: %s", self.path)
        return {"date": self._today(), "used": 0, "calls": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @property
    def used(self) -> int:
        return int(self._state["used"])

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    def can_afford(self, endpoint: str, n: int = 1) -> bool:
        return self.remaining >= config.QUOTA_COST.get(endpoint, 1) * n

    def charge(self, endpoint: str, n: int = 1) -> None:
        cost = config.QUOTA_COST.get(endpoint, 1) * n
        if cost > self.remaining:
            raise QuotaExceeded(
                f"쿼터 부족: {endpoint} 호출에 {cost} units 필요, "
                f"남은 예산 {self.remaining}/{self.budget} units. "
                "내일 다시 실행하면 이미 수집한 데이터부터 이어서 진행합니다."
            )
        self._state["used"] = self.used + cost
        self._state["calls"][endpoint] = self._state["calls"].get(endpoint, 0) + n
        self._save()

    def summary(self) -> str:
        calls = ", ".join(f"{k}={v}" for k, v in sorted(self._state["calls"].items()))
        return f"쿼터 {self.used}/{self.budget} units 사용 ({calls or '호출 없음'})"


class YouTubeClient:
    def __init__(
        self,
        api_key: str,
        cfg: config.CollectConfig | None = None,
        ledger: QuotaLedger | None = None,
    ):
        if not api_key:
            raise ValueError(
                f"{config.ENV_YOUTUBE_KEY} 환경변수가 비어 있습니다. "
                "docs/01_YouTube_API_키_발급가이드.md 를 참고하세요."
            )
        self.api_key = api_key
        self.cfg = cfg or config.CollectConfig()
        self.ledger = ledger or QuotaLedger()
        self.session = requests.Session()

    # ── 저수준 호출 ───────────────────────────────────────────────────
    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """endpoint 예: 'search' -> 쿼터 키는 'search.list'."""
        quota_key = f"{endpoint}.list"
        self.ledger.charge(quota_key)

        url = f"{API_BASE}/{endpoint}"
        payload = {**params, "key": self.api_key}
        delay = 2.0
        last_error: Exception | None = None

        for attempt in range(self.cfg.max_retries + 1):
            try:
                resp = self.session.get(url, params=payload, timeout=30)
            except requests.RequestException as exc:      # 네트워크 오류 -> 재시도
                last_error = exc
            else:
                if resp.status_code == 200:
                    time.sleep(self.cfg.request_sleep)
                    return resp.json()

                body = resp.text[:500]
                # 403 quotaExceeded 는 재시도해도 무의미하므로 즉시 중단
                if resp.status_code == 403 and "quota" in body.lower():
                    raise QuotaExceeded(
                        "YouTube API가 쿼터 초과를 반환했습니다. "
                        f"로컬 집계는 {self.ledger.used} units 입니다. 응답: {body}"
                    )
                if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                    raise YouTubeAPIError(
                        f"{endpoint}.list {resp.status_code}: {body}"
                    )
                last_error = YouTubeAPIError(f"{endpoint}.list {resp.status_code}: {body}")

            if attempt < self.cfg.max_retries:
                log.warning("%s 재시도 %d/%d (%.0fs 대기)", endpoint,
                            attempt + 1, self.cfg.max_retries, delay)
                time.sleep(delay)
                delay *= 2

        raise YouTubeAPIError(f"{endpoint}.list 재시도 모두 실패") from last_error

    # ── 채널 탐색 ─────────────────────────────────────────────────────
    def search_channels(self, query: str, use_cache: bool = True) -> list[dict[str, Any]]:
        """회사명으로 채널 후보를 검색한다 (100 units — 반드시 캐시)."""
        key = hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]
        cache_file = config.SEARCH_CACHE / f"{key}.json"
        if use_cache and cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))["items"]

        data = self._get("search", {
            "part": "snippet",
            "q": query,
            "type": "channel",
            "maxResults": self.cfg.search_candidates,
            "regionCode": self.cfg.region_code,
            "relevanceLanguage": self.cfg.relevance_language,
        })
        items = data.get("items", [])
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"query": query, "items": items}, ensure_ascii=False),
            encoding="utf-8",
        )
        return items

    def channels_by_id(self, channel_ids: Sequence[str]) -> list[dict[str, Any]]:
        """채널 ID로 상세 조회 (50개씩 묶어 1 unit — 가장 저렴한 경로)."""
        out: list[dict[str, Any]] = []
        for chunk in _chunks(channel_ids, 50):
            data = self._get("channels", {
                "part": "snippet,statistics,contentDetails,brandingSettings",
                "id": ",".join(chunk),
                "maxResults": 50,
            })
            out.extend(data.get("items", []))
        return out

    def channel_by_handle(self, handle: str) -> dict[str, Any] | None:
        """@핸들로 채널 조회 (1 unit). search.list 대비 100배 저렴."""
        data = self._get("channels", {
            "part": "snippet,statistics,contentDetails,brandingSettings",
            "forHandle": handle.lstrip("@"),
        })
        items = data.get("items", [])
        return items[0] if items else None

    # ── 영상 수집 ─────────────────────────────────────────────────────
    def uploads_video_ids(self, uploads_playlist_id: str, limit: int) -> list[str]:
        """업로드 재생목록에서 최신순으로 영상 ID를 limit개까지 가져온다."""
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < limit:
            params = {
                "part": "contentDetails",
                "playlistId": uploads_playlist_id,
                "maxResults": min(50, limit - len(ids)),
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._get("playlistItems", params)
            for item in data.get("items", []):
                vid = item.get("contentDetails", {}).get("videoId")
                if vid:
                    ids.append(vid)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return ids[:limit]

    def videos_by_id(self, video_ids: Sequence[str]) -> list[dict[str, Any]]:
        """영상 상세 조회 (50개씩 1 unit)."""
        out: list[dict[str, Any]] = []
        for chunk in _chunks(video_ids, 50):
            data = self._get("videos", {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(chunk),
                "maxResults": 50,
            })
            out.extend(data.get("items", []))
        return out


def _chunks(seq: Sequence[str], size: int) -> Iterator[list[str]]:
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def estimate_quota(n_companies: int, n_channels: int, videos_per_channel: int) -> dict[str, int]:
    """실행 전 쿼터 소요를 추정한다. 193개 기준 설계 근거가 되는 계산."""
    search = n_companies * config.QUOTA_COST["search.list"]
    channels = -(-n_channels // 50) * config.QUOTA_COST["channels.list"]
    pages = -(-videos_per_channel // 50)
    playlist = n_channels * pages * config.QUOTA_COST["playlistItems.list"]
    videos = n_channels * pages * config.QUOTA_COST["videos.list"]
    return {
        "채널탐색(search.list)": search,
        "채널상세(channels.list)": channels,
        "업로드목록(playlistItems.list)": playlist,
        "영상상세(videos.list)": videos,
        "합계": search + channels + playlist + videos,
        "필요_일수": -(-(search + channels + playlist + videos) // config.DAILY_QUOTA),
    }
