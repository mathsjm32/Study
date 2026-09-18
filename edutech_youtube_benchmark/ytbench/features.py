"""3단계-A: 파생 지표 계산.

'배울 점 / 부족한 점'을 데이터로 말하려면 조회수 절대값만으로는 부족하다.
규모가 큰 채널은 당연히 조회수가 높기 때문에, 규모를 보정한 효율 지표와
채널 내부 편차 지표가 있어야 비교가 성립한다. 그래서 두 층으로 만든다.

  영상 단위 (video_features) : 길이·제목·참여율·업로드 타이밍
  채널 단위 (channel_metrics): 위를 집계 + 규모 보정 + 일관성/모멘텀
"""

from __future__ import annotations

import logging
import re
from typing import Any

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger(__name__)

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


# ── 영상 단위 ─────────────────────────────────────────────────────────
def build_video_features(videos: pd.DataFrame) -> pd.DataFrame:
    """영상 원본 -> 영상 파생 지표."""
    if videos.empty:
        return videos.copy()

    df = videos.copy()

    # 업로드 시각을 KST로 — 한국 시청자 기준 요일·시간대 분석을 위해
    published = pd.to_datetime(df["published_at"], errors="coerce", utc=True)
    df["published_kst"] = published.dt.tz_convert(config.KST)
    df["upload_weekday"] = df["published_kst"].dt.weekday.map(
        lambda w: WEEKDAY_KR[int(w)] if pd.notna(w) else ""
    )
    df["upload_hour"] = df["published_kst"].dt.hour
    now = pd.Timestamp.now(tz=config.KST)
    df["days_since_upload"] = (now - df["published_kst"]).dt.total_seconds() / 86400

    # 포맷 분류 — 숏폼/롱폼 전략은 완전히 다른 게임이라 반드시 분리한다
    df["format"] = pd.cut(
        df["duration_sec"],
        bins=[-1, config.SHORTS_MAX_SECONDS, config.MID_MAX_SECONDS, np.inf],
        labels=["숏폼", "미들폼", "롱폼"],
    ).astype(str)
    df.loc[df["duration_sec"] <= 0, "format"] = "미분류"

    # 참여 지표 — 조회수 0 나눗셈 방지, 비공개 지표는 NaN으로 남긴다
    views = df["view_count"].replace(0, np.nan)
    df["like_rate"] = np.where(df["likes_hidden"], np.nan, df["like_count"] / views)
    df["comment_rate"] = np.where(
        df["comments_disabled"], np.nan, df["comment_count"] / views
    )
    df["engagement_rate"] = df[["like_rate", "comment_rate"]].sum(
        axis=1, min_count=1
    )

    # 조회 속도 — 오래된 영상이 누적으로 유리해지는 편향을 줄인다
    df["views_per_day"] = df["view_count"] / df["days_since_upload"].clip(lower=1)

    # 제목 지표
    df["title_len"] = df["title"].fillna("").str.len()
    df["title_word_count"] = df["title"].fillna("").str.split().str.len()
    for name, pattern in config.TITLE_PATTERNS.items():
        df[f"제목_{name}"] = df["title"].fillna("").str.contains(
            pattern, regex=True, na=False
        )
    for group, words in config.HOOK_KEYWORDS.items():
        rx = "|".join(re.escape(w) for w in words)
        df[f"후킹_{group}"] = df["title"].fillna("").str.contains(rx, regex=True, na=False)

    df["hook_type_count"] = df[[f"후킹_{g}" for g in config.HOOK_KEYWORDS]].sum(axis=1)
    df["has_tags"] = df["tags"].fillna("").str.len() > 0
    df["tag_count"] = df["tags"].fillna("").apply(lambda s: len(s.split("|")) if s else 0)

    # 채널 내부 상대 성과 — '이 채널 기준으로' 잘된 영상인지
    med = df.groupby("channel_id")["view_count"].transform("median")
    df["views_vs_channel_median"] = df["view_count"] / med.replace(0, np.nan)
    df["is_hit"] = df["views_vs_channel_median"] >= config.HIT_MULTIPLIER
    df["is_flop"] = df["views_vs_channel_median"] <= (1 / config.HIT_MULTIPLIER)

    return df


# ── 채널 단위 ─────────────────────────────────────────────────────────
def _upload_cadence(group: pd.DataFrame) -> dict[str, float]:
    """업로드 빈도와 규칙성.

    규칙성은 업로드 간격의 변동계수(CV)로 본다. CV가 낮으면 '매주 화요일'처럼
    예측 가능한 채널이고, 높으면 몰아서 올리다 쉬는 채널이다. 유튜브
    구독 유지에는 예측 가능성이 유리하므로 별도 지표로 둔다.
    """
    dates = group["published_kst"].dropna().sort_values()
    if len(dates) < 2:
        return {"upload_interval_median_days": np.nan, "upload_regularity_cv": np.nan}
    gaps = dates.diff().dropna().dt.total_seconds() / 86400
    mean = gaps.mean()
    return {
        "upload_interval_median_days": float(gaps.median()),
        "upload_regularity_cv": float(gaps.std() / mean) if mean > 0 else np.nan,
    }


def _momentum(group: pd.DataFrame) -> float:
    """최근 90일 중위 조회수 / 직전 90일 중위 조회수.

    1.0 초과면 성장, 미만이면 둔화. 표본이 부족하면 NaN.
    """
    w = config.MOMENTUM_WINDOW_DAYS
    recent = group.loc[group["days_since_upload"] <= w, "view_count"]
    prior = group.loc[
        (group["days_since_upload"] > w) & (group["days_since_upload"] <= 2 * w),
        "view_count",
    ]
    if len(recent) < 2 or len(prior) < 2:
        return np.nan
    base = prior.median()
    return float(recent.median() / base) if base > 0 else np.nan


def _mode_or_blank(series: pd.Series) -> Any:
    s = series.dropna()
    if s.empty:
        return ""
    m = s.mode()
    return m.iloc[0] if not m.empty else ""


def build_channel_metrics(
    channels: pd.DataFrame, video_features: pd.DataFrame
) -> pd.DataFrame:
    """채널 원본 + 영상 파생 -> 채널 단위 지표 한 행/채널."""
    if channels.empty:
        return channels.copy()

    rows: list[dict[str, Any]] = []
    grouped = dict(tuple(video_features.groupby("channel_id"))) if not video_features.empty else {}

    for _, ch in channels.iterrows():
        cid = ch["channel_id"]
        g = grouped.get(cid, pd.DataFrame(columns=video_features.columns))
        n = len(g)

        rec: dict[str, Any] = {
            "company_name": ch["company_name"],
            "category": ch.get("category", ""),
            "channel_id": cid,
            "channel_title": ch["channel_title"],
            "handle": ch.get("handle", ""),
            "channel_url": f"https://www.youtube.com/channel/{cid}",
            "match_method": ch.get("match_method", ""),
            "match_confidence": ch.get("match_confidence", 0),
            "subscriber_count": ch["subscriber_count"],
            "subscriber_hidden": ch.get("subscriber_hidden", False),
            "channel_view_count": ch["channel_view_count"],
            "channel_video_count": ch["channel_video_count"],
            "channel_created": ch.get("channel_created", ""),
            "videos_sampled": n,
        }

        # 규모 보정 지표 — 구독자 수가 다른 채널을 같은 자리에서 비교하려면 필수
        subs = ch["subscriber_count"]
        rec["avg_views_per_video_lifetime"] = (
            ch["channel_view_count"] / ch["channel_video_count"]
            if ch["channel_video_count"] else np.nan
        )
        rec["views_per_subscriber"] = (
            ch["channel_view_count"] / subs if subs else np.nan
        )

        if n == 0:
            rows.append(rec)
            continue

        # 조회수 분포 — 평균은 히트 1편에 휘둘리므로 중위수를 주 지표로 쓴다
        rec.update({
            "median_views": float(g["view_count"].median()),
            "mean_views": float(g["view_count"].mean()),
            "p90_views": float(g["view_count"].quantile(0.9)),
            "max_views": int(g["view_count"].max()),
            "views_dispersion_cv": float(
                g["view_count"].std() / g["view_count"].mean()
            ) if g["view_count"].mean() > 0 else np.nan,
            "median_views_per_subscriber": float(g["view_count"].median() / subs)
            if subs else np.nan,
            "median_views_per_day": float(g["views_per_day"].median()),
            "hit_ratio": float(g["is_hit"].mean()),
            "flop_ratio": float(g["is_flop"].mean()),
        })

        # 참여 지표
        rec.update({
            "engagement_rate_median": float(g["engagement_rate"].median(skipna=True)),
            "like_rate_median": float(g["like_rate"].median(skipna=True)),
            "comment_rate_median": float(g["comment_rate"].median(skipna=True)),
            "comments_disabled_ratio": float(g["comments_disabled"].mean()),
        })

        # 업로드 활동
        rec.update(_upload_cadence(g))
        rec["uploads_last_90d"] = int((g["days_since_upload"] <= config.RECENT_WINDOW_DAYS).sum())
        rec["uploads_last_30d"] = int((g["days_since_upload"] <= 30).sum())
        rec["days_since_last_upload"] = float(g["days_since_upload"].min())
        rec["momentum_90d"] = _momentum(g)

        # 콘텐츠 포맷 믹스
        fmt = g["format"].value_counts(normalize=True)
        for label in ("숏폼", "미들폼", "롱폼"):
            rec[f"{label}_비율"] = float(fmt.get(label, 0.0))
        rec["duration_median_sec"] = float(g["duration_sec"].median())

        # 포맷별 성과 — '숏폼을 해야 하나'에 답하는 근거
        for label in ("숏폼", "미들폼", "롱폼"):
            sub = g[g["format"] == label]
            rec[f"{label}_중위조회수"] = float(sub["view_count"].median()) if len(sub) else np.nan

        # 제목 전략
        rec["title_len_median"] = float(g["title_len"].median())
        for name in config.TITLE_PATTERNS:
            rec[f"제목_{name}_비율"] = float(g[f"제목_{name}"].mean())
        for group_name in config.HOOK_KEYWORDS:
            rec[f"후킹_{group_name}_비율"] = float(g[f"후킹_{group_name}"].mean())
        rec["태그_사용률"] = float(g["has_tags"].mean())
        rec["태그수_중위"] = float(g["tag_count"].median())

        # 업로드 타이밍
        rec["최다_업로드요일"] = _mode_or_blank(g["upload_weekday"])
        rec["최다_업로드시간"] = _mode_or_blank(g["upload_hour"])

        # 대표 영상 — 리포트/LLM 프롬프트에 그대로 쓰인다
        best = g.nlargest(1, "view_count")
        if not best.empty:
            rec["top_video_title"] = best.iloc[0]["title"]
            rec["top_video_views"] = int(best.iloc[0]["view_count"])
            rec["top_video_url"] = f"https://youtu.be/{best.iloc[0]['video_id']}"

        rows.append(rec)

    return pd.DataFrame(rows)
