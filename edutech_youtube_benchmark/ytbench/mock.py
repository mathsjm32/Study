"""API 키 없이 파이프라인 전체를 검증하기 위한 모의 데이터 생성기.

실제 YouTube API 응답과 동일한 JSON 구조를 만들기 때문에, collect.py
이후의 모든 단계(파싱·지표·스코어·리포트)가 실제 데이터에서와 같은
경로로 실행된다. 키를 발급받은 뒤에는 `--mock` 없이 돌리면 그대로
실데이터로 전환된다.

구독자 수는 멱법칙(소수 대형 채널 + 다수 소형 채널) 분포를 따르게 하고,
일부 제목 패턴에는 의도적으로 성과 상관을 심어 두었다. 패턴 리프트
계산이 실제로 신호를 잡아내는지 확인하기 위한 장치다.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config

SEGMENTS = ["입시/인강", "어학", "코딩교육", "유아/초등", "자격증/취업",
            "성인교육/클래스", "학습솔루션/SaaS", "학원/오프라인"]

_NAME_PARTS_A = ["메가", "에듀", "클래스", "스마트", "아이", "테크", "코드", "링크",
                 "플랜", "노바", "브릿지", "퀀텀", "그로우", "모두", "리드", "베이직"]
_NAME_PARTS_B = ["스터디", "윌", "런", "캠퍼스", "스쿨", "랩", "에듀", "클래스",
                 "튜터", "아카데미", "트랙", "박스", "메이트", "노트"]

_TOPIC = ["수능 국어", "중등 수학", "토익 리스닝", "파이썬 기초", "코딩테스트",
          "영어회화", "초등 독서", "공무원 행정법", "면접 준비", "생성형 AI 활용",
          "내신 대비", "자기소개서", "엑셀 실무", "데이터 분석", "학습 습관"]

_HOOK_HIGH = ["3개월 만에 1등급 만든 방법", "합격자가 말하는 실수 3가지",
              "총정리 [무료 자료 포함]", "이것만 알면 점수 20점 오릅니다",
              "현직 강사가 알려주는 전략", "2026 개정 완벽 대비 가이드"]
_HOOK_LOW = ["안내드립니다", "소개 영상", "공지사항", "인터뷰 full",
             "브랜드 필름", "설명회 다시보기"]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(
    n_companies: int = 193,
    seed: int = 20260918,
    out_channels=None,
    out_videos=None,
) -> tuple[int, int]:
    """모의 채널/영상 JSONL을 생성한다. Returns: (채널 수, 영상 수)."""
    from .collect import CHANNELS_JSONL, VIDEOS_JSONL

    out_channels = out_channels or CHANNELS_JSONL
    out_videos = out_videos or VIDEOS_JSONL
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)

    channels: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []

    used_names: set[str] = set()
    for i in range(n_companies):
        while True:
            name = rng.choice(_NAME_PARTS_A) + rng.choice(_NAME_PARTS_B)
            if name not in used_names:
                used_names.add(name)
                break
        cid = f"UCmock{i:017d}"
        segment = rng.choice(SEGMENTS)

        # 멱법칙 구독자 분포: 대부분 1천~5만, 상위 5%가 50만~300만
        if rng.random() < 0.05:
            subs = rng.randint(500_000, 3_000_000)
        elif rng.random() < 0.25:
            subs = rng.randint(50_000, 500_000)
        else:
            subs = rng.randint(300, 50_000)

        # 채널 유형: 활발/보통/방치 — 일관성·최신성 축을 구분하기 위한 장치
        archetype = rng.choices(
            ["활발", "보통", "방치", "숏폼중심"], weights=[0.25, 0.45, 0.2, 0.1]
        )[0]
        cadence = {"활발": 3, "보통": 11, "방치": 45, "숏폼중심": 2}[archetype]
        n_videos = {"활발": rng.randint(60, 100), "보통": rng.randint(25, 70),
                    "방치": rng.randint(5, 30), "숏폼중심": rng.randint(70, 100)}[archetype]
        last_upload_gap = {"활발": rng.randint(0, 7), "보통": rng.randint(3, 30),
                           "방치": rng.randint(120, 600), "숏폼중심": rng.randint(0, 5)}[archetype]

        base_views = max(50, int(subs * rng.uniform(0.01, 0.25)))
        created = now - timedelta(days=rng.randint(400, 3600))

        channels.append({
            "id": cid,
            "snippet": {
                "title": f"{name} 공식채널",
                "description": f"{segment} 전문 에듀테크 브랜드 {name}의 공식 유튜브입니다. "
                               f"강의, 학습 콘텐츠, 합격 후기를 제공합니다.",
                "customUrl": f"@{name.lower()}",
                "country": "KR",
                "publishedAt": _iso(created),
            },
            "statistics": {
                "subscriberCount": str(subs),
                "viewCount": str(base_views * n_videos * rng.randint(2, 8)),
                "videoCount": str(n_videos + rng.randint(0, 200)),
                "hiddenSubscriberCount": False,
            },
            "contentDetails": {"relatedPlaylists": {"uploads": f"UUmock{i:017d}"}},
            "_company_name": name,
            "_category": segment,
            "_match_method": rng.choices(["search", "handle", "id_direct"],
                                         weights=[0.7, 0.2, 0.1])[0],
            "_confidence": round(rng.uniform(0.55, 1.0), 3),
        })

        upload_at = now - timedelta(days=last_upload_gap)
        for v in range(n_videos):
            # 의도적 상관: 후킹 제목은 조회수 배수를 더 크게 받는다
            strong = rng.random() < 0.45
            hook = rng.choice(_HOOK_HIGH if strong else _HOOK_LOW)
            topic = rng.choice(_TOPIC)
            title = f"[{topic}] {hook}" if rng.random() < 0.5 else f"{topic} {hook}"
            if archetype == "숏폼중심" and rng.random() < 0.7:
                title += " #shorts"
                duration = rng.randint(15, 59)
            else:
                duration = rng.choice([rng.randint(15, 59), rng.randint(70, 590),
                                       rng.randint(600, 2400)])

            multiplier = rng.lognormvariate(0.35 if strong else 0.0, 0.9)
            views = max(10, int(base_views * multiplier))
            like_rate = rng.uniform(0.008, 0.06) * (1.25 if strong else 1.0)
            comment_rate = rng.uniform(0.0004, 0.006)

            videos.append({
                "id": f"vid{i:04d}{v:04d}",
                "snippet": {
                    "channelId": cid,
                    "channelTitle": f"{name} 공식채널",
                    "title": title,
                    "description": f"{topic} 관련 콘텐츠입니다. 자세한 내용은 홈페이지 참고.",
                    "publishedAt": _iso(upload_at),
                    "tags": [topic, segment, "에듀테크"] if rng.random() < 0.6 else [],
                    "categoryId": "27",
                    "thumbnails": {
                        "high": {"url": f"https://i.ytimg.com/vi/vid{i:04d}{v:04d}/hqdefault.jpg"},
                        "maxres": {"url": f"https://i.ytimg.com/vi/vid{i:04d}{v:04d}/maxresdefault.jpg"},
                    },
                },
                "statistics": {
                    "viewCount": str(views),
                    "likeCount": str(int(views * like_rate)),
                    "commentCount": str(int(views * comment_rate)),
                },
                "contentDetails": {
                    "duration": f"PT{duration // 60}M{duration % 60}S",
                    "definition": "hd",
                    "caption": "true" if rng.random() < 0.3 else "false",
                },
                "_company_name": name,
                "_channel_id": cid,
                "_category": segment,
            })

            # 업로드 시각도 흔들어야 요일·시간대 분석이 의미를 갖는다.
            # 실제 채널처럼 저녁 시간대에 몰리도록 가중치를 준다.
            hour = rng.choices(range(24), weights=[1]*6 + [3]*6 + [5]*6 + [8]*6)[0]
            upload_at = upload_at.replace(hour=hour, minute=rng.randint(0, 59))
            upload_at -= timedelta(days=max(1, int(rng.gauss(cadence, cadence * 0.5))))

    for path, rows in ((out_channels, channels), (out_videos, videos)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return len(channels), len(videos)


def generate_companies_csv(n: int = 193, seed: int = 20260918) -> None:
    """모의 경쟁사 목록 CSV — 입력 포맷 확인용."""
    from .collect import CHANNELS_JSONL, _read_jsonl

    rows = _read_jsonl(CHANNELS_JSONL)[:n]
    config.COMPANIES_CSV.parent.mkdir(parents=True, exist_ok=True)
    with config.COMPANIES_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        f.write("company_name,channel_url,category,note\n")
        for r in rows:
            f.write(f"{r['_company_name']},,{r['_category']},모의데이터\n")
