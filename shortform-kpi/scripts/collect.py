"""수집 실행 CLI.

    python scripts/collect.py youtube              # 최근 3일 (기본, 재수집)
    python scripts/collect.py youtube --days 90    # 90일 소급 수집
    python scripts/collect.py all                  # 구현된 모든 플랫폼

몇 번을 실행해도 같은 결과가 되도록 모든 적재는 UPSERT 로 이뤄진다.
YouTube 수치는 2~3일간 확정되지 않으므로 기본값이 최근 3일 재수집이다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import INITIAL_BACKFILL_DAYS  # noqa: E402
from src.collectors import base  # noqa: E402

PLATFORMS = ("youtube", "instagram", "facebook")


def run_youtube(days: int | None) -> int:
    from src.collectors import youtube

    return base.run_collector(
        "youtube", lambda result: youtube.collect(result, days=days)
    )


def run_instagram(days: int | None) -> int:
    from src.collectors import instagram

    return base.run_collector(
        "instagram", lambda result: instagram.collect(result, days=days)
    )


def run_facebook(days: int | None) -> int:
    from src.collectors import facebook

    return base.run_collector(
        "facebook", lambda result: facebook.collect(result, days=days)
    )


RUNNERS = {
    "youtube": run_youtube,
    "instagram": run_instagram,
    "facebook": run_facebook,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="숏폼 지표 수집")
    parser.add_argument(
        "platform", choices=(*PLATFORMS, "all"), help="수집할 플랫폼"
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help=(
            "되짚어 볼 기간(일). "
            "YouTube 는 지표 날짜 범위(기본 최근 3일 재수집), "
            "Instagram/Facebook 은 대상 콘텐츠의 게시일 범위"
            f"(기본 {INITIAL_BACKFILL_DAYS}일)를 뜻한다."
        ),
    )
    args = parser.parse_args()

    targets = PLATFORMS if args.platform == "all" else (args.platform,)
    codes = [RUNNERS[name](args.days) for name in targets]

    failed = [name for name, code in zip(targets, codes) if code != 0]
    if failed:
        print(f"\n실패한 플랫폼: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
