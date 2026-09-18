"""3단계-B: 벤치마크 스코어 + 성공 패턴 역산.

두 가지를 한다.

1) 성공 패턴 학습 (learn_success_patterns)
   "제목에 숫자를 넣어라" 같은 통념을 그대로 점수화하지 않는다. 대신
   수집된 전체 영상에서 각 패턴의 리프트(패턴 있는 영상의 채널상대조회수
   중위수 / 없는 영상의 중위수)를 계산해, 에듀테크 업계에서 실제로 먹히는
   패턴만 골라낸다. 이 리프트 표 자체가 '배울 점'의 1차 근거가 된다.

2) 6축 스코어 (build_scores)
   절대값 비교는 규모 큰 채널에 유리하므로, 모든 지표를 193개 모집단 내
   백분위(0~100)로 변환한 뒤 가중합한다. 점수는 '업계 내 상대 위치'다.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger(__name__)

MIN_PATTERN_SAMPLE = 30      # 리프트를 신뢰하기 위한 최소 영상 수


def _pct(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """모집단 내 백분위(0~100). NaN은 중앙값(50)으로 둬 과도한 벌점을 막는다."""
    s = pd.to_numeric(series, errors="coerce")
    if s.notna().sum() <= 1:
        return pd.Series(50.0, index=series.index)
    ranked = s.rank(pct=True, na_option="keep") * 100
    if not higher_is_better:
        ranked = 100 - ranked
    return ranked.fillna(50.0)


# ── 1) 성공 패턴 역산 ─────────────────────────────────────────────────
def learn_success_patterns(video_features: pd.DataFrame) -> pd.DataFrame:
    """각 패턴/포맷의 성과 리프트를 계산한다.

    비교 대상은 절대 조회수가 아니라 views_vs_channel_median(채널 내부
    상대 조회수)이다. 대형 채널이 특정 패턴을 많이 쓴다는 사실만으로
    그 패턴이 좋아 보이는 편향을 제거하기 위함이다.
    """
    if video_features.empty:
        return pd.DataFrame()

    df = video_features
    target = "views_vs_channel_median"
    rows: list[dict[str, Any]] = []

    flag_cols = (
        [c for c in df.columns if c.startswith("제목_")]
        + [c for c in df.columns if c.startswith("후킹_")]
        + ["has_tags"]
    )

    for col in flag_cols:
        on = df.loc[df[col] == True, target].dropna()       # noqa: E712
        off = df.loc[df[col] == False, target].dropna()     # noqa: E712
        if len(on) < MIN_PATTERN_SAMPLE or len(off) < MIN_PATTERN_SAMPLE:
            continue
        base = off.median()
        if not base or base <= 0:
            continue
        rows.append({
            "구분": "제목패턴" if col.startswith("제목_") else
                    ("후킹유형" if col.startswith("후킹_") else "메타데이터"),
            "패턴": col.replace("제목_", "").replace("후킹_", "")
                      .replace("has_tags", "태그사용"),
            "사용영상수": int(len(on)),
            "사용률": round(len(on) / (len(on) + len(off)), 4),
            "패턴적용_상대조회수중위": round(float(on.median()), 4),
            "미적용_상대조회수중위": round(float(base), 4),
            "리프트": round(float(on.median() / base), 4),
        })

    # 포맷(숏폼/미들폼/롱폼)은 플래그가 아니라 범주형이라 따로 처리
    for label in ("숏폼", "미들폼", "롱폼"):
        on = df.loc[df["format"] == label, target].dropna()
        off = df.loc[(df["format"] != label) & (df["format"] != "미분류"), target].dropna()
        if len(on) < MIN_PATTERN_SAMPLE or len(off) < MIN_PATTERN_SAMPLE:
            continue
        base = off.median()
        if not base or base <= 0:
            continue
        rows.append({
            "구분": "포맷", "패턴": label, "사용영상수": int(len(on)),
            "사용률": round(len(on) / len(df), 4),
            "패턴적용_상대조회수중위": round(float(on.median()), 4),
            "미적용_상대조회수중위": round(float(base), 4),
            "리프트": round(float(on.median() / base), 4),
        })

    out = pd.DataFrame(rows)
    if out.empty:
        log.warning("패턴 표본이 부족해 리프트를 계산하지 못했습니다 "
                    "(패턴별 최소 %d개 영상 필요)", MIN_PATTERN_SAMPLE)
        return out
    return out.sort_values("리프트", ascending=False).reset_index(drop=True)


def winning_patterns(lift_table: pd.DataFrame, min_lift: float = 1.05) -> list[str]:
    """리프트가 유의미하게 1을 넘는 제목/후킹 패턴 이름 목록."""
    if lift_table.empty:
        return []
    mask = (lift_table["리프트"] >= min_lift) & (
        lift_table["구분"].isin(["제목패턴", "후킹유형", "메타데이터"])
    )
    return lift_table.loc[mask, "패턴"].tolist()


def _craft_score(metrics: pd.DataFrame, winners: list[str]) -> pd.Series:
    """검증된 성공 패턴을 얼마나 활용하는지 (0~100).

    폴백: 패턴 학습이 불가능할 만큼 표본이 적으면 제목 길이 적정성과
    태그 사용률만으로 대체 점수를 만든다.
    """
    usable = [f"제목_{w}_비율" for w in winners if f"제목_{w}_비율" in metrics.columns]
    usable += [f"후킹_{w}_비율" for w in winners if f"후킹_{w}_비율" in metrics.columns]
    if "태그사용" in winners and "태그_사용률" in metrics.columns:
        usable.append("태그_사용률")

    if usable:
        return pd.concat([_pct(metrics[c]) for c in usable], axis=1).mean(axis=1)

    log.info("검증된 성공 패턴이 없어 제목길이·태그 기반 대체 점수를 사용합니다")
    parts = []
    if "title_len_median" in metrics:
        # 한국어 유튜브 제목은 25~45자 구간이 검색·노출 모두에 무난하다
        dist = (metrics["title_len_median"] - 35).abs()
        parts.append(_pct(dist, higher_is_better=False))
    if "태그_사용률" in metrics:
        parts.append(_pct(metrics["태그_사용률"]))
    return pd.concat(parts, axis=1).mean(axis=1) if parts else pd.Series(50.0, index=metrics.index)


# ── 2) 6축 스코어 ─────────────────────────────────────────────────────
def build_scores(
    channel_metrics: pd.DataFrame, lift_table: pd.DataFrame | None = None
) -> pd.DataFrame:
    """채널 지표 -> 6축 백분위 점수 + 총점 + 등급."""
    if channel_metrics.empty:
        return channel_metrics.copy()

    m = channel_metrics.copy()
    winners = winning_patterns(lift_table) if lift_table is not None else []

    axes = pd.DataFrame(index=m.index)

    # 도달력 — 순수 규모
    axes["도달력"] = pd.concat([
        _pct(m["subscriber_count"]),
        _pct(m["channel_view_count"]),
    ], axis=1).mean(axis=1)

    # 효율성 — 규모를 보정한 실제 성과
    eff = [_pct(m["views_per_subscriber"])]
    if "median_views_per_subscriber" in m:
        eff.append(_pct(m["median_views_per_subscriber"]))
    if "median_views_per_day" in m:
        eff.append(_pct(m["median_views_per_day"]))
    axes["효율성"] = pd.concat(eff, axis=1).mean(axis=1)

    # 참여도 — 좋아요+댓글 비율. 히트율로 보강해 '반응의 폭'까지 반영
    eng = [_pct(m.get("engagement_rate_median", pd.Series(np.nan, index=m.index)))]
    if "hit_ratio" in m:
        eng.append(_pct(m["hit_ratio"]))
    axes["참여도"] = pd.concat(eng, axis=1).mean(axis=1)

    # 일관성 — 자주(빈도) + 예측 가능하게(CV 낮을수록 좋음)
    cons = [_pct(m.get("uploads_last_90d", pd.Series(np.nan, index=m.index)))]
    if "upload_regularity_cv" in m:
        cons.append(_pct(m["upload_regularity_cv"], higher_is_better=False))
    axes["일관성"] = pd.concat(cons, axis=1).mean(axis=1)

    # 콘텐츠설계 — 검증된 성공 패턴 활용도
    axes["콘텐츠설계"] = _craft_score(m, winners)

    # 최신성 — 지금도 돌아가는 채널인가
    rec = [_pct(m.get("days_since_last_upload", pd.Series(np.nan, index=m.index)),
                higher_is_better=False)]
    if "uploads_last_30d" in m:
        rec.append(_pct(m["uploads_last_30d"]))
    axes["최신성"] = pd.concat(rec, axis=1).mean(axis=1)

    for axis, weight in config.SCORE_WEIGHTS.items():
        m[f"점수_{axis}"] = axes[axis].round(1)
    m["종합점수"] = sum(
        axes[axis] * weight for axis, weight in config.SCORE_WEIGHTS.items()
    ).round(1)

    m["업계순위"] = m["종합점수"].rank(ascending=False, method="min").astype("Int64")
    m["등급"] = pd.cut(
        m["종합점수"],
        bins=[-1, 30, 45, 60, 75, 101],
        labels=["D(하위)", "C(평균이하)", "B(평균)", "A(상위)", "S(최상위)"],
    ).astype(str)

    # 강·약점 자동 태깅 — 리포트에서 바로 읽히는 한 줄 진단
    axis_cols = list(config.SCORE_WEIGHTS)
    m["최강축"] = axes[axis_cols].idxmax(axis=1)
    m["최약축"] = axes[axis_cols].idxmin(axis=1)
    m["진단"] = [
        f"{s} 우위 / {w} 취약" for s, w in zip(m["최강축"], m["최약축"])
    ]

    return m.sort_values("종합점수", ascending=False).reset_index(drop=True)


def industry_summary(scores: pd.DataFrame, videos: pd.DataFrame) -> pd.DataFrame:
    """업계 전체 벤치마크 기준선 — '우리가 어디에 서 있나'의 기준값."""
    def q(col: str, p: float) -> float:
        if col not in scores or scores[col].dropna().empty:
            return float("nan")
        return round(float(scores[col].quantile(p)), 4)

    items = [
        ("분석 채널 수", len(scores), "개"),
        ("분석 영상 수", len(videos), "개"),
        ("구독자 중위값", q("subscriber_count", 0.5), "명"),
        ("구독자 상위25% 기준", q("subscriber_count", 0.75), "명"),
        ("영상당 중위 조회수(중위 채널)", q("median_views", 0.5), "회"),
        ("영상당 중위 조회수(상위25% 채널)", q("median_views", 0.75), "회"),
        ("구독자당 조회수 중위값", q("views_per_subscriber", 0.5), "배"),
        ("참여율 중위값", q("engagement_rate_median", 0.5), "비율"),
        ("참여율 상위25% 기준", q("engagement_rate_median", 0.75), "비율"),
        ("90일 업로드 수 중위값", q("uploads_last_90d", 0.5), "개"),
        ("90일 업로드 수 상위25% 기준", q("uploads_last_90d", 0.75), "개"),
        ("업로드 간격 중위값", q("upload_interval_median_days", 0.5), "일"),
        ("숏폼 비율 중위값", q("숏폼_비율", 0.5), "비율"),
        ("영상 길이 중위값", q("duration_median_sec", 0.5), "초"),
        ("제목 길이 중위값", q("title_len_median", 0.5), "자"),
    ]
    return pd.DataFrame(items, columns=["지표", "값", "단위"])
