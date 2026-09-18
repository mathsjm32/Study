"""2단계: 채널 메타 + 최근 영상 수집.

매칭된 채널마다
  channels.list -> uploads 재생목록 ID -> playlistItems.list -> videos.list
순으로 내려가며, 영상 단위 원본 테이블을 만든다.

채널당 100개 영상 기준 4 units (채널 1 + 목록 2 + 상세 2 미만)이라
193개 채널 전체가 약 800 units — 하루 예산의 8% 수준이다.
비싼 쪽은 1단계 검색이고, 이 단계는 반복 실행해도 부담이 없다.

이어받기(resume): 이미 수집된 channel_id는 건너뛴다. 쿼터 소진이나
네트워크 오류로 중단돼도 다음 실행에서 남은 채널만 처리한다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config
from .resolve import Resolution
from .youtube_client import QuotaExceeded, YouTubeAPIError, YouTubeClient

log = logging.getLogger(__name__)

CHANNELS_JSONL = config.RAW / "channels.jsonl"
VIDEOS_JSONL = config.RAW / "videos.jsonl"


def _append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("JSONL 파싱 실패(1행 건너뜀): %s", path.name)
    return out


def collected_channel_ids(path: Path = VIDEOS_JSONL) -> set[str]:
    """이미 수집이 끝난 채널 — 이어받기 판단용."""
    return {r.get("_channel_id", "") for r in _read_jsonl(path)} - {""}


def collect_all(
    resolutions: list[Resolution],
    client: YouTubeClient,
    cfg: config.CollectConfig | None = None,
    resume: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """매칭된 채널 전체에서 영상을 수집한다.

    Returns: (채널 레코드, 영상 레코드) — 원본 JSON에 회사명을 덧붙인 형태.
    """
    cfg = cfg or config.CollectConfig()
    targets = [r for r in resolutions if r.channel_id]
    skip = collected_channel_ids() if resume else set()
    todo = [r for r in targets if r.channel_id not in skip]

    log.info("수집 대상 %d채널 (완료 %d채널 건너뜀), 채널당 최대 %d영상",
             len(todo), len(targets) - len(todo), cfg.max_videos_per_channel)

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=cfg.lookback_days)
        if cfg.lookback_days else None
    )

    by_id = {r.channel_id: r for r in todo}
    channel_rows: list[dict[str, Any]] = []
    video_rows: list[dict[str, Any]] = []

    # 채널 상세는 50개씩 묶어서 한 번에 (1 unit)
    try:
        channel_items = client.channels_by_id(list(by_id))
    except QuotaExceeded:
        log.error("쿼터 소진 — 채널 상세 조회 불가. 내일 재실행하세요.")
        return [], []

    for item in channel_items:
        r = by_id[item["id"]]
        row = {**item, "_company_name": r.company_name, "_category": r.category,
               "_match_method": r.match_method, "_confidence": r.confidence}
        channel_rows.append(row)

    _append_jsonl(CHANNELS_JSONL, channel_rows)

    for idx, ch in enumerate(channel_rows, 1):
        cid = ch["id"]
        company = ch["_company_name"]
        uploads = (ch.get("contentDetails", {})
                     .get("relatedPlaylists", {})
                     .get("uploads", ""))
        if not uploads:
            log.warning("[%d/%d] %s — 업로드 재생목록 없음(영상 0개 채널)",
                        idx, len(channel_rows), company)
            continue

        try:
            vids = client.uploads_video_ids(uploads, cfg.max_videos_per_channel)
            items = client.videos_by_id(vids) if vids else []
        except QuotaExceeded:
            log.warning("[%d/%d] 쿼터 소진 — %s 이후 중단. 진행분은 저장됨.",
                        idx, len(channel_rows), company)
            break
        except YouTubeAPIError as exc:
            log.warning("[%d/%d] %s 수집 실패(건너뜀): %s", idx, len(channel_rows),
                        company, exc)
            continue

        kept = []
        for v in items:
            published = v.get("snippet", {}).get("publishedAt")
            if cutoff and published:
                try:
                    if datetime.fromisoformat(published.replace("Z", "+00:00")) < cutoff:
                        continue        # lookback_days 밖의 영상은 제외
                except ValueError:
                    pass
            kept.append({**v, "_company_name": company, "_channel_id": cid,
                         "_category": ch.get("_category", "")})

        video_rows.extend(kept)
        _append_jsonl(VIDEOS_JSONL, kept)
        log.info("[%d/%d] %s — 영상 %d개 수집 (%s)",
                 idx, len(channel_rows), company, len(kept), client.ledger.summary())

    return channel_rows, video_rows


# ── 원본 JSON -> 평탄한 테이블 ────────────────────────────────────────
def _int(value: Any) -> int:
    """statistics 필드는 문자열이고, 비공개면 아예 키가 없다."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_duration(iso: str) -> int:
    """ISO-8601 기간(PT1H2M3S)을 초로 변환. 라이브/누락은 0."""
    import re
    if not iso or not iso.startswith("P"):
        return 0
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", iso)
    if not m:
        return 0
    d, h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return int(d * 86400 + h * 3600 + mi * 60 + s)


def channels_to_frame(rows: list[dict[str, Any]] | None = None):
    import pandas as pd
    rows = rows if rows is not None else _read_jsonl(CHANNELS_JSONL)
    recs = []
    for c in rows:
        snip, stats = c.get("snippet", {}), c.get("statistics", {})
        recs.append({
            "company_name": c.get("_company_name", ""),
            "category": c.get("_category", ""),
            "channel_id": c.get("id", ""),
            "channel_title": snip.get("title", ""),
            "handle": (snip.get("customUrl") or "").lstrip("@"),
            "channel_desc": (snip.get("description") or "").replace("\n", " ")[:500],
            "country": snip.get("country", ""),
            "channel_created": snip.get("publishedAt", ""),
            "subscriber_count": _int(stats.get("subscriberCount")),
            "subscriber_hidden": bool(stats.get("hiddenSubscriberCount")),
            "channel_view_count": _int(stats.get("viewCount")),
            "channel_video_count": _int(stats.get("videoCount")),
            "match_method": c.get("_match_method", ""),
            "match_confidence": c.get("_confidence", 0),
        })
    df = pd.DataFrame(recs)
    # 같은 채널이 여러 번 수집됐으면 최신 1건만 남긴다
    return df.drop_duplicates(subset="channel_id", keep="last") if not df.empty else df


def videos_to_frame(rows: list[dict[str, Any]] | None = None):
    import pandas as pd
    rows = rows if rows is not None else _read_jsonl(VIDEOS_JSONL)
    recs = []
    for v in rows:
        snip = v.get("snippet", {})
        stats = v.get("statistics", {})
        details = v.get("contentDetails", {})
        recs.append({
            "company_name": v.get("_company_name", ""),
            "category": v.get("_category", ""),
            "channel_id": v.get("_channel_id", "") or snip.get("channelId", ""),
            "channel_title": snip.get("channelTitle", ""),
            "video_id": v.get("id", ""),
            "title": snip.get("title", ""),
            "description": (snip.get("description") or "").replace("\n", " ")[:1000],
            "published_at": snip.get("publishedAt", ""),
            "tags": "|".join(snip.get("tags", []) or []),
            "category_id": snip.get("categoryId", ""),
            "thumbnail_url": _best_thumbnail(snip.get("thumbnails", {})),
            "duration_sec": parse_duration(details.get("duration", "")),
            "definition": details.get("definition", ""),
            "caption": details.get("caption", ""),
            "view_count": _int(stats.get("viewCount")),
            "like_count": _int(stats.get("likeCount")),
            "comment_count": _int(stats.get("commentCount")),
            # 좋아요/댓글이 꺼진 영상은 키 자체가 없다 -> 0과 구분해 기록
            "likes_hidden": "likeCount" not in stats,
            "comments_disabled": "commentCount" not in stats,
        })
    df = pd.DataFrame(recs)
    return df.drop_duplicates(subset="video_id", keep="last") if not df.empty else df


def _best_thumbnail(thumbs: dict[str, Any]) -> str:
    for size in ("maxres", "standard", "high", "medium", "default"):
        if url := thumbs.get(size, {}).get("url"):
            return url
    return ""


def save_frames() -> tuple[Path, Path]:
    """원본 JSONL -> CSV 두 개로 정리."""
    config.PROCESSED.mkdir(parents=True, exist_ok=True)
    ch, vi = channels_to_frame(), videos_to_frame()
    ch.to_csv(config.CHANNELS_CSV, index=False, encoding="utf-8-sig")
    vi.to_csv(config.VIDEOS_CSV, index=False, encoding="utf-8-sig")
    log.info("저장 완료 — 채널 %d행, 영상 %d행", len(ch), len(vi))
    return config.CHANNELS_CSV, config.VIDEOS_CSV
