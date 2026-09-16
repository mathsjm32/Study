"""Meta API 가 실제로 무엇을 돌려주는지 그대로 찍어 본다.

    python scripts/probe_meta.py instagram
    python scripts/probe_meta.py facebook

수집기는 지표 이름을 실행 시점에 협상하지만, 그 결과가 기대와 다를 때
(저장수가 비어 있다든지) 원본 응답을 봐야 원인을 알 수 있다. 이 스크립트는
콘텐츠 한 건에 대해 필드·인사이트 응답을 그대로 출력한다.
BigQuery 에는 아무것도 쓰지 않는다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (  # noqa: E402
    FB_REELS_METRICS,
    FB_VIDEO_FIELDS,
    IG_MEDIA_METRICS,
    META,
)
from src import meta  # noqa: E402


def dump(label: str, payload) -> None:
    print(f"\n{'=' * 64}\n{label}\n{'=' * 64}")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])


def probe_instagram() -> int:
    META.validate_instagram()
    fields = ("id,caption,media_type,media_product_type,permalink,timestamp,"
              "like_count,comments_count")
    page = meta.get(f"{META.ig_user_id}/media", fields=fields, limit=25)
    reels = [m for m in page.get("data", []) if m.get("media_product_type") == "REELS"]
    if not reels:
        print("최근 25개 미디어 중 릴스가 없습니다. limit 을 늘려 보세요.")
        return 1

    target = reels[0]
    dump("1. 미디어 객체 (릴스 1건)", target)

    print(f"\n{'=' * 64}\n2. 지표 협상 — 후보 {len(IG_MEDIA_METRICS)}개\n{'=' * 64}")
    working = meta.resolve_metrics(f"{target['id']}/insights", IG_MEDIA_METRICS)
    print(f"\n사용 가능: {working}")
    print(f"제외됨   : {[m for m in IG_MEDIA_METRICS if m not in working]}")

    if working:
        raw = meta.get(f"{target['id']}/insights", metric=",".join(working))
        dump("3. 인사이트 원본 응답", raw)
        dump("4. 파싱 결과 {지표: 값}", meta.insight_values(raw))
    return 0


def probe_facebook() -> int:
    META.validate_facebook()
    for edge in (f"{META.fb_page_id}/video_reels", f"{META.fb_page_id}/videos"):
        name = edge.split("/")[-1]
        try:
            fields = meta.resolve_fields(edge, FB_VIDEO_FIELDS, limit=1)
            print(f"\n[엣지 {name}] 사용 가능 필드: {fields}")
            page = meta.get(edge, fields=",".join(fields), limit=10)
        except meta.MetaError as error:
            print(f"\n[엣지 {name}] 사용 불가: {error}")
            continue

        videos = page.get("data", [])
        if not videos:
            print(f"[엣지 {name}] 영상이 없습니다.")
            continue

        target = videos[0]
        dump(f"1. 영상 객체 (엣지 {name})", target)

        print(f"\n{'=' * 64}\n2. 지표 협상 — 후보 {len(FB_REELS_METRICS)}개\n{'=' * 64}")
        working = meta.resolve_metrics(f"{target['id']}/video_insights", FB_REELS_METRICS)
        print(f"\n사용 가능: {working}")
        print(f"제외됨   : {[m for m in FB_REELS_METRICS if m not in working]}")

        if working:
            raw = meta.get(f"{target['id']}/video_insights", metric=",".join(working))
            dump("3. 인사이트 원본 응답", raw)
            dump("4. 파싱 결과 {지표: 값}", meta.insight_values(raw))
        return 0

    print("\n영상 목록을 가져올 수 있는 엣지가 없습니다. 토큰 권한을 확인하세요.")
    return 1


PROBES = {"instagram": probe_instagram, "facebook": probe_facebook}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in PROBES:
        print(f"사용법: python scripts/probe_meta.py [{' | '.join(PROBES)}]")
        return 2
    try:
        return PROBES[sys.argv[1]]()
    except Exception as exc:
        print(f"\n실패: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
