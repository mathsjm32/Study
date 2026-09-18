"""1단계: 회사명 -> 유튜브 채널 매칭.

193개 회사명만 있는 상태에서 가장 큰 리스크는 '엉뚱한 채널을 경쟁사로
집계하는 것'이다. 따라서 자동 매칭에 신뢰도 점수를 붙이고, 애매한 건은
needs_review 플래그로 걸러 사람이 15분 안에 검수할 수 있게 만든다.

쿼터 주의: search.list는 1건당 100 units. 193건 = 19,300 units 로
하루 예산(10,000)을 넘긴다. 그래서
  · 회사명 검색 결과는 디스크에 영구 캐시 (재실행 시 0 units)
  · channel_url/handle이 입력에 있으면 channels.list(1 unit) 로 우회
  · 쿼터가 바닥나면 진행분을 저장하고 중단 -> 다음 날 이어서 실행
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from . import config
from .youtube_client import QuotaExceeded, YouTubeClient

log = logging.getLogger(__name__)

# 회사명 정규화 시 제거할 법인 형태·수식어
_NOISE = re.compile(
    r"\((주|유|재|사)\)|주식회사|\b(inc|corp|co|ltd|llc|kr)\b|[㈜（）()\[\]{}·・,\.\-_/'\"]|\s+",
    re.IGNORECASE,
)

# 에듀테크 채널임을 시사하는 단어 (설명·채널명에서 가점)
EDUTECH_SIGNALS = [
    "교육", "학습", "강의", "수업", "학원", "인강", "공부", "에듀", "edu",
    "학생", "학부모", "입시", "수능", "내신", "코딩", "영어", "수학", "자격증",
    "취업", "커리어", "온라인클래스", "스터디", "튜터", "과외", "학교",
]


@dataclass
class Company:
    """입력 CSV 1행. company_name만 필수, 나머지는 있으면 쿼터를 크게 절약."""

    company_name: str
    aliases: str = ""        # 파이프(|) 구분. 영문명·브랜드명 등 같은 회사의 다른 표기
    channel_url: str = ""
    handle: str = ""
    channel_id: str = ""
    category: str = ""
    note: str = ""

    def name_variants(self) -> list[str]:
        """검색어 + 별칭. 채널명 유사도 채점에 모두 사용한다."""
        out = [self.company_name]
        out += [a.strip() for a in (self.aliases or "").split("|") if a.strip()]
        return out


@dataclass
class Resolution:
    company_name: str
    category: str
    channel_id: str
    channel_title: str
    handle: str
    match_method: str       # id_direct | handle | search | failed
    confidence: float       # 0.0 ~ 1.0
    needs_review: bool
    candidates: str         # 검수용: 후보 채널명 나열
    reason: str


# ── 입력 로딩 ─────────────────────────────────────────────────────────
def load_companies(path: Path = config.COMPANIES_CSV) -> list[Company]:
    """경쟁사 CSV를 읽는다. 컬럼명은 유연하게 매핑한다."""
    if not path.exists():
        raise FileNotFoundError(
            f"경쟁사 목록이 없습니다: {path}\n"
            "data/companies_template.csv 를 companies.csv 로 복사한 뒤 "
            "193개 회사명을 채워주세요."
        )

    alias = {
        "company_name": {"company_name", "회사명", "기업명", "company", "name", "경쟁사"},
        "aliases": {"aliases", "별칭", "별명", "alias", "영문명", "다른이름"},
        "channel_url": {"channel_url", "url", "채널url", "채널주소", "링크", "link"},
        "handle": {"handle", "핸들", "@handle"},
        "channel_id": {"channel_id", "채널id", "channelid"},
        "category": {"category", "분류", "카테고리", "업종", "세그먼트"},
        "note": {"note", "비고", "메모"},
    }

    def field_for(header: str) -> str | None:
        h = header.strip().lower().lstrip("﻿")
        for field, names in alias.items():
            if h in names:
                return field
        return None

    companies: list[Company] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        mapping = {h: field_for(h) for h in (reader.fieldnames or [])}
        if "company_name" not in mapping.values():
            raise ValueError(
                f"회사명 컬럼을 찾을 수 없습니다. 헤더: {reader.fieldnames}\n"
                "'company_name' 또는 '회사명' 컬럼이 필요합니다."
            )
        for row in reader:
            kwargs: dict[str, str] = {}
            for header, field in mapping.items():
                if field:
                    kwargs[field] = (row.get(header) or "").strip()
            if kwargs.get("company_name"):
                companies.append(Company(**kwargs))

    # 회사명 중복 제거 (앞선 행 우선)
    seen: set[str] = set()
    unique: list[Company] = []
    for c in companies:
        k = normalize(c.company_name)
        if k and k not in seen:
            seen.add(k)
            unique.append(c)
    if len(unique) != len(companies):
        log.info("중복 회사명 %d건 제거", len(companies) - len(unique))
    return unique


def normalize(name: str) -> str:
    return _NOISE.sub("", (name or "").lower())


# ── 매칭 점수 ─────────────────────────────────────────────────────────
def name_similarity(company: str | Iterable[str], channel_title: str) -> float:
    """회사명(또는 별칭 목록) 중 채널명과 가장 잘 맞는 하나의 유사도.

    별칭을 쓰는 이유: 한글 사명과 영문 채널명이 다른 경우(뤼이드 <-> Riiid)
    단일 문자열 비교로는 유사도가 0이 되어 자동 매칭이 전부 실패한다.
    별칭 비교는 API를 더 호출하지 않으므로 쿼터 비용이 0이다.
    """
    if not isinstance(company, str):
        return max((name_similarity(c, channel_title) for c in company), default=0.0)
    a, b = normalize(company), normalize(channel_title)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # 포함 관계면 부분 문자열 길이 비율을 하한으로 보정
    # ("메가스터디" vs "메가스터디교육 공식채널" 같은 케이스)
    if a in b or b in a:
        ratio = max(ratio, len(min(a, b, key=len)) / len(max(a, b, key=len)), 0.75)
    return round(ratio, 4)


def edutech_affinity(text: str) -> float:
    """설명·제목에 교육 관련 단어가 얼마나 있는지 (0.0~1.0)."""
    low = (text or "").lower()
    hits = sum(1 for kw in EDUTECH_SIGNALS if kw in low)
    return min(1.0, hits / 4.0)


def score_candidate(company: str | Iterable[str], item: dict[str, Any]) -> tuple[float, str]:
    """검색 후보 1건의 신뢰도와 근거를 계산한다. company는 별칭 목록도 가능."""
    snip = item.get("snippet", {})
    title = snip.get("channelTitle") or snip.get("title") or ""
    desc = snip.get("description", "")

    sim = name_similarity(company, title)
    aff = edutech_affinity(f"{title} {desc}")
    # 이름 유사도가 지배적이어야 한다 — 교육 관련성은 동점자 판별용 보조 지표
    score = 0.8 * sim + 0.2 * aff
    reason = f"이름유사도 {sim:.2f}, 교육관련성 {aff:.2f}"
    return round(score, 4), reason


# ── 메인 파이프라인 ───────────────────────────────────────────────────
CONFIDENCE_OK = 0.70        # 이상이면 자동 확정
CONFIDENCE_REVIEW = 0.45    # 이상~OK 미만이면 검수 권장, 미만이면 실패 처리


def extract_from_url(url: str) -> tuple[str, str]:
    """채널 URL에서 (channel_id, handle)을 뽑는다."""
    if not url:
        return "", ""
    if m := re.search(r"/channel/(UC[\w-]{20,})", url):
        return m.group(1), ""
    if m := re.search(r"youtube\.com/@([\w.\-]+)", url):
        return "", m.group(1)
    if m := re.search(r"youtube\.com/(?:c|user)/([\w.\-]+)", url):
        return "", m.group(1)      # 레거시 경로 — 핸들로 시도
    return "", ""


def resolve_all(
    companies: list[Company],
    client: YouTubeClient,
    stop_on_quota: bool = True,
) -> list[Resolution]:
    """회사 목록 전체를 채널로 매칭한다. 쿼터 소진 시 진행분까지 반환."""
    results: list[Resolution] = []

    # ① ID/핸들이 이미 있는 회사는 저렴한 경로로 먼저 처리
    direct_ids: dict[str, Company] = {}
    needs_search: list[Company] = []
    handle_targets: list[tuple[Company, str]] = []

    for c in companies:
        cid, handle = c.channel_id.strip(), c.handle.strip().lstrip("@")
        if not cid and not handle:
            cid, handle = extract_from_url(c.channel_url)
        if cid:
            direct_ids[cid] = c
        elif handle:
            handle_targets.append((c, handle))
        else:
            needs_search.append(c)

    log.info("매칭 경로 분배 — ID직접 %d건, 핸들 %d건, 검색필요 %d건 (검색 예상 %d units)",
             len(direct_ids), len(handle_targets), len(needs_search),
             len(needs_search) * config.QUOTA_COST["search.list"])

    # ID 직접 조회 (50개당 1 unit)
    if direct_ids:
        for item in client.channels_by_id(list(direct_ids)):
            c = direct_ids[item["id"]]
            results.append(_resolution_from_channel(c, item, "id_direct", 1.0,
                                                    "입력에 채널 ID가 명시됨"))
        found = {r.channel_id for r in results}
        for cid, c in direct_ids.items():
            if cid not in found:
                results.append(_failed(c, f"채널 ID {cid} 조회 실패(삭제/비공개 가능)"))

    # 핸들 조회 (1건당 1 unit)
    for c, handle in handle_targets:
        try:
            item = client.channel_by_handle(handle)
        except QuotaExceeded:
            if stop_on_quota:
                log.warning("쿼터 소진 — 핸들 매칭 중단, 진행분 %d건 저장", len(results))
                return results
            raise
        if item:
            results.append(_resolution_from_channel(c, item, "handle", 0.95,
                                                    f"@{handle} 핸들 일치"))
        else:
            needs_search.append(c)   # 핸들 실패 시 검색으로 폴백

    # ② 검색 (1건당 100 units) — 가장 비싼 단계, 마지막에 배치
    for c in needs_search:
        try:
            items = client.search_channels(c.company_name)
        except QuotaExceeded:
            if stop_on_quota:
                log.warning(
                    "쿼터 소진 — 검색 %d건 남음. 진행분 %d건을 저장합니다. "
                    "내일 재실행하면 캐시된 건은 0 units로 건너뜁니다.",
                    len(needs_search) - len([r for r in results if r.match_method == "search"]),
                    len(results),
                )
                return results
            raise

        if not items:
            results.append(_failed(c, "검색 결과 0건 — 유튜브 채널 미보유 가능"))
            continue

        variants = c.name_variants()
        scored = sorted(
            ((*score_candidate(variants, it), it) for it in items),
            key=lambda t: t[0],
            reverse=True,
        )
        best_score, best_reason, best = scored[0]
        cand_names = " | ".join(
            f"{it.get('snippet', {}).get('channelTitle', '?')}({s:.2f})"
            for s, _, it in scored[:3]
        )

        if best_score < CONFIDENCE_REVIEW:
            results.append(_failed(
                c, f"최고 후보 신뢰도 {best_score:.2f} < {CONFIDENCE_REVIEW} — 자동매칭 포기",
                candidates=cand_names))
            continue

        cid = best.get("id", {}).get("channelId", "")
        snip = best.get("snippet", {})
        results.append(Resolution(
            company_name=c.company_name,
            category=c.category,
            channel_id=cid,
            channel_title=snip.get("channelTitle") or snip.get("title", ""),
            handle="",
            match_method="search",
            confidence=best_score,
            needs_review=best_score < CONFIDENCE_OK,
            candidates=cand_names,
            reason=best_reason,
        ))

    return results


def _resolution_from_channel(
    c: Company, item: dict[str, Any], method: str, confidence: float, reason: str
) -> Resolution:
    snip = item.get("snippet", {})
    return Resolution(
        company_name=c.company_name,
        category=c.category,
        channel_id=item.get("id", ""),
        channel_title=snip.get("title", ""),
        handle=snip.get("customUrl", "").lstrip("@"),
        match_method=method,
        confidence=confidence,
        needs_review=False,
        candidates="",
        reason=reason,
    )


def _failed(c: Company, reason: str, candidates: str = "") -> Resolution:
    return Resolution(
        company_name=c.company_name, category=c.category, channel_id="",
        channel_title="", handle="", match_method="failed", confidence=0.0,
        needs_review=True, candidates=candidates, reason=reason,
    )


def save_resolutions(rows: Iterable[Resolution], path: Path = config.RESOLVED_CSV) -> Path:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()) if rows
                                else [f.name for f in Resolution.__dataclass_fields__.values()])
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))
    return path


def load_resolutions(path: Path = config.RESOLVED_CSV) -> list[Resolution]:
    if not path.exists():
        return []
    out: list[Resolution] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            out.append(Resolution(
                company_name=row["company_name"],
                category=row.get("category", ""),
                channel_id=row.get("channel_id", ""),
                channel_title=row.get("channel_title", ""),
                handle=row.get("handle", ""),
                match_method=row.get("match_method", ""),
                confidence=float(row.get("confidence") or 0),
                needs_review=str(row.get("needs_review", "")).lower() in ("true", "1", "yes"),
                candidates=row.get("candidates", ""),
                reason=row.get("reason", ""),
            ))
    return out
