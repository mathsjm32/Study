"""탐색적 분석용 차트.

색상 규칙 (검증된 팔레트):
  · 크기 비교(순위·히트맵) = 단일 색조 light->dark 시퀀셜
  · 극성 비교(리프트 1.0 기준) = blue<->red 다이버징 + 회색 중립점
  · 계열 구분 = 고정 순서 3색(blue/orange/aqua)까지만 사용
무지개 색상, 이중 y축, 모든 점에 숫자 붙이기는 쓰지 않는다.
"""

from __future__ import annotations

import glob
import logging
import platform

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── 색상 슬롯 ─────────────────────────────────────────────────────────
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SUB = "#52514e"
INK_MUTED = "#8a8985"
GRID = "#e6e5e1"

SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]      # 고정 순서, 순환 금지
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#256abf",
       "#1c5cab", "#184f95", "#104281"]         # blue 100->650
DIV_POS, DIV_NEG, DIV_MID = "#2a78d6", "#d03b3b", "#f0efec"

# 한국어 환경에서 우선 시도할 폰트 (Windows -> macOS -> Linux)
KOREAN_FONTS = ["Malgun Gothic", "맑은 고딕", "AppleGothic", "Apple SD Gothic Neo",
                "NanumGothic", "NanumBarunGothic", "Noto Sans CJK KR",
                "Noto Sans KR", "Pretendard", "Gulim"]


def setup_korean_font() -> str | None:
    """한글 폰트를 찾아 matplotlib 기본값으로 설정한다.

    폰트가 없으면 라벨이 모두 □□□로 렌더링되므로, 조용히 실패하지 않고
    설치 방법을 경고로 남긴다.
    """
    from matplotlib import font_manager

    # 리눅스에서 apt로 설치한 폰트는 캐시에 없을 수 있어 직접 등록한다
    for pattern in ("/usr/share/fonts/**/Nanum*.ttf",
                    "/usr/share/fonts/**/NotoSansCJK*.ttc",
                    "/usr/share/fonts/**/NotoSansKR*.ttf"):
        for path in glob.glob(pattern, recursive=True):
            try:
                font_manager.fontManager.addfont(path)
            except (RuntimeError, OSError):
                pass

    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in KOREAN_FONTS:
        if name in available:
            mpl.rcParams["font.family"] = name
            mpl.rcParams["axes.unicode_minus"] = False   # 한글 폰트는 −(U+2212) 누락
            log.info("한글 폰트 적용: %s", name)
            return name

    system = platform.system()
    hint = {
        "Windows": "Windows에는 'Malgun Gothic'이 기본 설치돼 있습니다. "
                   "matplotlib 폰트 캐시를 지워보세요: "
                   "matplotlib.font_manager._load_fontmanager(try_read_cache=False)",
        "Darwin": "macOS에는 'AppleGothic'이 기본 설치돼 있습니다.",
    }.get(system, "설치: sudo apt-get install -y fonts-nanum (Debian/Ubuntu)")
    log.warning("한글 폰트를 찾지 못해 차트 라벨이 깨질 수 있습니다. %s", hint)
    return None


def apply_style() -> None:
    """차트 전역 스타일 — 격자·축은 후퇴시키고 데이터를 앞세운다."""
    setup_korean_font()
    mpl.rcParams.update({
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "figure.dpi": 110,
        "savefig.dpi": 140,
        "axes.edgecolor": GRID,
        "axes.linewidth": 0.8,
        "axes.labelcolor": INK_SUB,
        "axes.titlesize": 13,
        "axes.titleweight": "bold",
        "axes.titlecolor": INK,
        "axes.titlepad": 14,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "xtick.color": INK_SUB,
        "ytick.color": INK_SUB,
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "grid.color": GRID,
        "grid.linewidth": 0.7,
        "legend.frameon": False,
        "legend.fontsize": 9,
        "lines.linewidth": 2.0,
        "lines.markersize": 8,
    })


def _title(ax, title: str, subtitle: str | None = None) -> None:
    """제목 + 부제. 부제가 있으면 제목을 더 띄워 겹침을 막는다."""
    ax.set_title(title, loc="left", pad=30 if subtitle else 12)
    if subtitle:
        ax.text(0, 1.012, subtitle, transform=ax.transAxes, ha="left", va="bottom",
                fontsize=9, color=INK_MUTED)


def _seq_colors(values: pd.Series, lo: int = 2, hi: int = 8) -> list[str]:
    """값의 크기를 시퀀셜 램프 구간에 매핑한다."""
    v = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    if np.all(np.isnan(v)) or np.nanmax(v) == np.nanmin(v):
        return [SEQ[hi - 1]] * len(v)
    norm = (v - np.nanmin(v)) / (np.nanmax(v) - np.nanmin(v))
    idx = np.clip((norm * (hi - lo)).round().astype(int) + lo, 0, len(SEQ) - 1)
    return [SEQ[i] for i in idx]


# ── 1. 채널 랭킹 ──────────────────────────────────────────────────────
def plot_top_channels(scores: pd.DataFrame, n: int = 20, ax=None):
    """종합점수 상위 n개 채널 — 크기 비교이므로 단일 색조 시퀀셜."""
    d = scores.nlargest(n, "종합점수").iloc[::-1]
    if ax is None:
        _, ax = plt.subplots(figsize=(9, max(4, n * 0.33)))

    y = np.arange(len(d))
    # 인접 막대 사이 2px 간격 -> height 0.72
    ax.barh(y, d["종합점수"], height=0.72, color=_seq_colors(d["종합점수"]),
            edgecolor="none")
    ax.set_yticks(y, d["company_name"], fontsize=9)
    ax.tick_params(axis="y", length=0)
    ax.set_xlim(0, max(100, d["종합점수"].max() * 1.16))
    ax.xaxis.grid(True, alpha=0.7)
    ax.set_axisbelow(True)
    ax.set_xlabel("종합점수 (경쟁사 집단 내 백분위 가중합)")

    # 직접 라벨 — 축을 읽으러 눈이 왕복하지 않게
    for yi, (val, grade) in enumerate(zip(d["종합점수"], d["등급"])):
        ax.text(val + 1.2, yi, f"{val:.1f}  {grade}", va="center",
                fontsize=8.5, color=INK_SUB)

    _title(ax, f"종합점수 상위 {n}개 채널",
           "규모가 아닌 6축 백분위 가중합 · 효율성(구독자당 성과) 25% 반영")
    return ax


# ── 2. 6축 비교 ───────────────────────────────────────────────────────
def plot_axis_comparison(scores: pd.DataFrame, companies: list[str] | None = None, ax=None):
    """선택 채널 vs 업계 중위의 6축 비교.

    레이더 차트를 쓰지 않는 이유: 축 순서에 따라 면적이 달라져 실제보다
    강하거나 약해 보이고, 축 간 값 비교도 어렵다. 그룹 막대가 정확하다.
    """
    axes_names = [c.replace("점수_", "") for c in scores.columns if c.startswith("점수_")]
    cols = [f"점수_{a}" for a in axes_names]

    if companies is None:
        companies = scores.nlargest(3, "종합점수")["company_name"].tolist()
    companies = companies[:3]               # 계열은 3개까지만 (색 분리 한계)

    if ax is None:
        _, ax = plt.subplots(figsize=(10, 5))

    x = np.arange(len(axes_names))
    width = 0.78 / len(companies)
    median = scores[cols].median()

    for i, company in enumerate(companies):
        row = scores.loc[scores["company_name"] == company, cols]
        if row.empty:
            continue
        offset = (i - (len(companies) - 1) / 2) * width
        bars = ax.bar(x + offset, row.iloc[0].to_numpy(dtype=float),
                      width=width * 0.88, color=SERIES[i], edgecolor="none",
                      label=company, zorder=3)
        for b, v in zip(bars, row.iloc[0]):
            ax.text(b.get_x() + b.get_width() / 2, v + 1.5, f"{v:.0f}",
                    ha="center", fontsize=7.5, color=INK_SUB, zorder=4)

    # 업계 중위는 계열이 아니라 기준선 — 회색 점선으로 후퇴시킨다
    for xi, val in zip(x, median):
        ax.plot([xi - 0.42, xi + 0.42], [val, val], color=INK_MUTED,
                lw=1.4, ls=(0, (3, 2)), zorder=5,
                label="업계 중위" if xi == 0 else None)

    ax.set_xticks(x, axes_names)
    ax.set_ylim(0, 108)
    ax.set_ylabel("백분위 점수")
    ax.yaxis.grid(True, alpha=0.7)
    ax.set_axisbelow(True)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.10), ncols=4)
    _title(ax, "6축 강·약점 비교", "점선 = 경쟁사 집단 중위값")
    return ax


# ── 3. 패턴 리프트 ────────────────────────────────────────────────────
def plot_pattern_lift(lift: pd.DataFrame, ax=None):
    """리프트 1.0을 기준으로 한 극성 비교 -> 다이버징 색상."""
    if lift.empty:
        if ax is None:
            _, ax = plt.subplots(figsize=(8, 2))
        ax.text(0.5, 0.5, "패턴 표본 부족 — 리프트 미산출", ha="center",
                va="center", color=INK_MUTED, transform=ax.transAxes)
        ax.axis("off")
        return ax

    d = lift.sort_values("리프트")
    if ax is None:
        _, ax = plt.subplots(figsize=(9, max(3.5, len(d) * 0.36)))

    y = np.arange(len(d))
    delta = d["리프트"] - 1.0
    colors = [DIV_POS if v >= 0 else DIV_NEG for v in delta]
    ax.barh(y, delta, height=0.72, color=colors, edgecolor="none", zorder=3)
    ax.axvline(0, color=INK_MUTED, lw=1.1, zorder=4)

    labels = [f"{r['패턴']}  ({r['구분']})" for _, r in d.iterrows()]
    ax.set_yticks(y, labels, fontsize=9)
    ax.tick_params(axis="y", length=0)
    span = max(abs(delta.min()), abs(delta.max())) * 1.30 or 0.1
    ax.set_xlim(-span, span)
    ax.xaxis.grid(True, alpha=0.7)
    ax.set_axisbelow(True)
    ax.set_xlabel("리프트 - 1.0  (오른쪽 = 성과에 기여)")

    for yi, (v, n) in enumerate(zip(delta, d["사용영상수"])):
        pad = span * 0.03
        ax.text(v + (pad if v >= 0 else -pad), yi,
                f"{v + 1:.2f}  (n={n:,})", va="center",
                ha="left" if v >= 0 else "right", fontsize=8, color=INK_SUB)

    _title(ax, "제목·포맷 패턴별 성과 리프트",
           "채널 내부 상대조회수 기준 — 대형 채널 편향을 제거한 값")
    return ax


# ── 4. 업로드 타이밍 히트맵 ───────────────────────────────────────────
def plot_timing_heatmap(video_features: pd.DataFrame, metric: str = "count", ax=None):
    """요일 × 시간대 — 크기 비교이므로 단일 색조 시퀀셜."""
    from .features import WEEKDAY_KR
    from matplotlib.colors import LinearSegmentedColormap

    pivot = video_features.pivot_table(
        index="upload_weekday", columns="upload_hour",
        values="views_vs_channel_median",
        aggfunc="size" if metric == "count" else "median",
    ).reindex(WEEKDAY_KR)
    pivot = pivot.reindex(columns=range(24))

    if ax is None:
        _, ax = plt.subplots(figsize=(12, 3.4))

    cmap = LinearSegmentedColormap.from_list("seq_blue", [SURFACE] + SEQ)
    data = pivot.to_numpy(dtype=float)
    im = ax.imshow(data, aspect="auto", cmap=cmap, interpolation="nearest")

    ax.set_xticks(range(24), [f"{h}" for h in range(24)])
    ax.set_yticks(range(len(WEEKDAY_KR)), WEEKDAY_KR)
    ax.set_xlabel("업로드 시간 (KST)")
    # 셀 경계 — 인접 채움 사이 2px 간격 규칙
    ax.set_xticks(np.arange(-0.5, 24, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(WEEKDAY_KR), 1), minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=1.6)
    ax.tick_params(which="minor", length=0)

    label = "업로드 건수" if metric == "count" else "채널내 상대조회수 중위"
    cbar = plt.colorbar(im, ax=ax, pad=0.012, fraction=0.03)
    cbar.set_label(label, fontsize=9, color=INK_SUB)
    cbar.outline.set_visible(False)

    _title(ax, f"업로드 타이밍 — {label}",
           "경쟁사 전체 합산 · 진한 칸이 몰리는 시간대")
    return ax


# ── 5. 효율성 산점도 ──────────────────────────────────────────────────
def plot_efficiency_scatter(scores: pd.DataFrame, annotate_n: int = 4, ax=None):
    """구독자 규모 vs 참여율 — 규모가 참여를 보장하지 않음을 보여준다."""
    d = scores[(scores["subscriber_count"] > 0)
               & scores["engagement_rate_median"].notna()].copy()
    if d.empty:
        if ax is None:
            _, ax = plt.subplots(figsize=(8, 5))
        ax.text(0.5, 0.5, "표본 없음", ha="center", va="center", transform=ax.transAxes)
        ax.axis("off")
        return ax

    if ax is None:
        _, ax = plt.subplots(figsize=(9, 5.6))

    # 단일 계열이므로 범례 불필요 — 제목이 계열을 지칭한다
    ax.scatter(d["subscriber_count"], d["engagement_rate_median"] * 100,
               s=42, color=SERIES[0], alpha=0.62,
               edgecolor=SURFACE, linewidth=1.4, zorder=3)
    ax.set_xscale("log")
    ax.set_xlabel("구독자 수 (로그 스케일)")
    ax.set_ylabel("참여율 중위 (%)")
    ax.grid(True, alpha=0.7)
    ax.set_axisbelow(True)

    med = d["engagement_rate_median"].median() * 100
    ax.axhline(med, color=INK_MUTED, lw=1.2, ls=(0, (3, 2)), zorder=2)
    # 기준선 라벨은 축 안쪽에 둔다 (축 밖에 두면 저장 시 잘린다)
    ax.text(0.995, med, f"업계 중위 {med:.2f}%", transform=ax.get_yaxis_transform(),
            ha="right", va="bottom", fontsize=8.5, color=INK_MUTED, zorder=6,
            bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1.5))

    # 눈에 띄는 소수만 라벨. 라벨끼리 겹치지 않게 위/아래로 번갈아 배치한다
    picks = d.nlargest(annotate_n, "engagement_rate_median").sort_values(
        "subscriber_count")
    for i, (_, r) in enumerate(picks.iterrows()):
        dy = 9 if i % 2 == 0 else -14
        ax.annotate(r["company_name"],
                    (r["subscriber_count"], r["engagement_rate_median"] * 100),
                    textcoords="offset points", xytext=(0, dy),
                    ha="center", fontsize=8, color=INK, zorder=6,
                    bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1.2))

    _title(ax, "규모 vs 참여율",
           "왼쪽 위 = 구독자는 적지만 반응이 강한 채널 (벤치마킹 1순위)")
    return ax


# ── 6. 포맷별 성과 ────────────────────────────────────────────────────
def plot_format_performance(video_features: pd.DataFrame, ax=None):
    """숏폼/미들폼/롱폼의 상대성과 분포 — 포맷 전환 판단 근거."""
    order = ["숏폼", "미들폼", "롱폼"]
    groups = [video_features.loc[video_features["format"] == f,
                                 "views_vs_channel_median"].dropna() for f in order]
    counts = [len(g) for g in groups]
    keep = [(f, g, n) for f, g, n in zip(order, groups, counts) if n >= 10]
    if not keep:
        if ax is None:
            _, ax = plt.subplots(figsize=(7, 4))
        ax.text(0.5, 0.5, "표본 부족", ha="center", va="center", transform=ax.transAxes)
        ax.axis("off")
        return ax

    if ax is None:
        _, ax = plt.subplots(figsize=(8, 4.6))

    bp = ax.boxplot([g for _, g, _ in keep], vert=True, widths=0.5,
                    showfliers=False, patch_artist=True,
                    medianprops=dict(color=INK, lw=1.8),
                    whiskerprops=dict(color=INK_MUTED, lw=1.1),
                    capprops=dict(color=INK_MUTED, lw=1.1))
    # 단일 계열 — 크기는 y축이 말해주므로 색으로 순위를 매기지 않는다
    for patch in bp["boxes"]:
        patch.set_facecolor(SEQ[3])
        patch.set_edgecolor(SURFACE)
        patch.set_linewidth(1.6)

    ax.set_xticks(range(1, len(keep) + 1),
                  [f"{f}\n(n={n:,})" for f, _, n in keep])
    ax.axhline(1.0, color=INK_MUTED, lw=1.1, ls=(0, (3, 2)))
    ax.set_ylabel("채널 중위 조회수 대비 배수")
    # 수염이 프레임에 잘리지 않도록 상한을 수염 기준으로 잡는다
    whisker_top = max(g.quantile(0.75) + 1.5 * (g.quantile(0.75) - g.quantile(0.25))
                      for _, g, _ in keep)
    ax.set_ylim(0, whisker_top * 1.08)
    ax.set_xlim(0.5, len(keep) + 0.5)
    ax.yaxis.grid(True, alpha=0.7)
    ax.set_axisbelow(True)
    ax.tick_params(axis="x", length=0)

    # 중위 라벨은 중위선 위에 겹치면 읽을 수 없으므로 상자 오른쪽 바깥에 둔다
    for i, (_, g, _) in enumerate(keep, 1):
        ax.text(i + 0.28, g.median(), f"중위 {g.median():.2f}", va="center",
                ha="left", fontsize=8.5, color=INK_SUB, zorder=6,
                bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1.5))

    _title(ax, "포맷별 상대성과 분포", "점선 1.0 = 채널 평균 수준 · 상자는 25~75%")
    return ax
