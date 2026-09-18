"""CLI 엔트리포인트.

기본 사용:
    python -m ytbench demo                  # 키 없이 전 과정 시연
    python -m ytbench estimate              # 쿼터·비용 사전 추정
    python -m ytbench resolve               # 회사명 -> 채널 매칭
    python -m ytbench collect               # 영상 수집 (이어받기 지원)
    python -m ytbench analyze               # 지표 + 스코어
    python -m ytbench llm                   # 정성 인사이트
    python -m ytbench report                # 엑셀 리포트
    python -m ytbench all                   # resolve~report 일괄
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from . import collect as collect_mod
from . import config, features, llm, report as report_mod, resolve as resolve_mod, score
from .youtube_client import QuotaExceeded, QuotaLedger, YouTubeClient, estimate_quota

log = logging.getLogger("ytbench")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _client(args) -> YouTubeClient:
    key = config.youtube_api_key()
    if not key:
        sys.exit(
            f"오류: {config.ENV_YOUTUBE_KEY} 환경변수가 없습니다.\n"
            "  발급 방법: docs/01_YouTube_API_키_발급가이드.md\n"
            "  키 없이 시연하려면: python -m ytbench demo"
        )
    cfg = config.CollectConfig(
        max_videos_per_channel=args.max_videos,
        lookback_days=args.lookback_days,
    )
    return YouTubeClient(key, cfg, QuotaLedger(budget=args.quota_budget))


# ── 서브커맨드 ────────────────────────────────────────────────────────
def cmd_estimate(args) -> None:
    companies = resolve_mod.load_companies(Path(args.companies)) if Path(args.companies).exists() else []
    n = len(companies) or args.companies_count

    have_url = sum(1 for c in companies if c.channel_url or c.handle or c.channel_id)
    need_search = n - have_url

    print(f"\n경쟁사 {n}개 (채널정보 보유 {have_url}개 / 검색필요 {need_search}개)\n")
    print("── YouTube API 쿼터 추정 ──────────────────────────────")
    est = estimate_quota(need_search, n, args.max_videos)
    for k, v in est.items():
        print(f"  {k:32s} {v:>8,}")
    print(f"\n  일일 무료 쿼터: {config.DAILY_QUOTA:,} units")
    if est["합계"] > config.DAILY_QUOTA:
        print(f"  ! 하루 예산 초과 -> {est['필요_일수']}일에 나눠 실행됩니다 "
              f"(중단 지점부터 자동 이어받기)")
        saved = need_search * config.QUOTA_COST["search.list"]
        print(f"  > companies.csv 의 channel_url 을 채우면 {saved:,} units 를 "
              f"절약해 하루에 끝낼 수 있습니다")
    else:
        print("  -> 하루 안에 전체 수집이 가능합니다")

    print("\n── LLM 정성 분석 비용 추정 ────────────────────────────")
    for thumbs in (False, True):
        cfg = config.LLMConfig(analyze_thumbnails=thumbs)
        e = llm.estimate_cost(n, cfg)
        label = "썸네일 포함" if thumbs else "텍스트만"
        print(f"  {label:12s} 표준 ${e['표준API_추정USD']:>6} / "
              f"Batch ${e['BatchAPI_추정USD']:>6}")
    print()


def cmd_resolve(args) -> None:
    companies = resolve_mod.load_companies(Path(args.companies))
    log.info("경쟁사 %d개 로드", len(companies))
    client = _client(args)

    # 이미 확정된 매칭은 건너뛴다 (쿼터 재소모 방지)
    existing = {r.company_name: r for r in resolve_mod.load_resolutions()}
    done = {name for name, r in existing.items()
            if r.channel_id and not r.needs_review}
    todo = [c for c in companies if c.company_name not in done]
    log.info("매칭 대상 %d개 (확정 %d개 건너뜀)", len(todo), len(done))

    results = resolve_mod.resolve_all(todo, client, stop_on_quota=True)
    merged = {**existing, **{r.company_name: r for r in results}}
    path = resolve_mod.save_resolutions(merged.values())

    rows = list(merged.values())
    ok = sum(1 for r in rows if r.channel_id and not r.needs_review)
    review = sum(1 for r in rows if r.needs_review and r.channel_id)
    failed = sum(1 for r in rows if not r.channel_id)
    log.info("매칭 완료 — 확정 %d / 검수필요 %d / 실패 %d -> %s",
             ok, review, failed, path)
    log.info(client.ledger.summary())
    if review or failed:
        log.warning("검수·실패 %d건은 리포트 '08_매칭검수' 시트에서 확인 후 "
                    "companies.csv 의 channel_url 을 채워 재실행하세요", review + failed)


def cmd_collect(args) -> None:
    resolutions = resolve_mod.load_resolutions()
    if not resolutions:
        sys.exit("오류: 매칭 결과가 없습니다. 먼저 `python -m ytbench resolve` 를 실행하세요.")
    client = _client(args)
    cfg = client.cfg
    try:
        collect_mod.collect_all(resolutions, client, cfg, resume=not args.no_resume)
    except QuotaExceeded as exc:
        log.warning("%s", exc)
    ch_path, vi_path = collect_mod.save_frames()
    log.info("%s", client.ledger.summary())
    log.info("저장 -> %s, %s", ch_path, vi_path)


def _load_analysis() -> tuple[pd.DataFrame, ...]:
    """analyze 결과를 디스크에서 읽는다 (llm/report 단계 공용)."""
    missing = [p for p in (config.VIDEO_FEATURES_CSV, config.CHANNEL_METRICS_CSV,
                           config.SCORES_CSV) if not p.exists()]
    if missing:
        sys.exit("오류: 분석 결과가 없습니다. 먼저 `python -m ytbench analyze` 를 실행하세요.\n"
                 + "\n".join(f"  없음: {p}" for p in missing))
    vf = pd.read_csv(config.VIDEO_FEATURES_CSV, encoding="utf-8-sig")
    cm = pd.read_csv(config.CHANNEL_METRICS_CSV, encoding="utf-8-sig")
    sc = pd.read_csv(config.SCORES_CSV, encoding="utf-8-sig")
    return vf, cm, sc


def cmd_analyze(args) -> None:
    ch = collect_mod.channels_to_frame()
    vi = collect_mod.videos_to_frame()
    if vi.empty:
        sys.exit("오류: 수집된 영상이 없습니다. `python -m ytbench collect` 또는 "
                 "`python -m ytbench demo` 를 먼저 실행하세요.")

    vf = features.build_video_features(vi)
    cm = features.build_channel_metrics(ch, vf)
    lift = score.learn_success_patterns(vf)
    sc = score.build_scores(cm, lift)

    config.PROCESSED.mkdir(parents=True, exist_ok=True)
    vf.to_csv(config.VIDEO_FEATURES_CSV, index=False, encoding="utf-8-sig")
    cm.to_csv(config.CHANNEL_METRICS_CSV, index=False, encoding="utf-8-sig")
    sc.to_csv(config.SCORES_CSV, index=False, encoding="utf-8-sig")
    lift.to_csv(config.PROCESSED / "pattern_lift.csv", index=False, encoding="utf-8-sig")

    log.info("분석 완료 — 채널 %d, 영상 %d, 패턴 %d개", len(cm), len(vf), len(lift))
    if not lift.empty:
        top = lift.head(5)[["구분", "패턴", "리프트"]].to_string(index=False)
        log.info("검증된 상위 패턴:\n%s", top)
    log.info("상위 5개 채널: %s", ", ".join(sc["company_name"].head(5)))


def cmd_llm(args) -> None:
    vf, cm, sc = _load_analysis()
    lift_path = config.PROCESSED / "pattern_lift.csv"
    lift = pd.read_csv(lift_path, encoding="utf-8-sig") if lift_path.exists() else pd.DataFrame()
    industry = score.industry_summary(sc, vf)

    cfg = config.LLMConfig(
        analyze_thumbnails=args.thumbnails,
        use_batch_api=not args.sync,
        max_channels=args.max_channels,
        effort=args.effort,
    )
    n = min(len(sc), cfg.max_channels or len(sc))
    est = llm.estimate_cost(n, cfg)
    log.info("LLM 분석 시작 — %d채널, 모델 %s, %s",
             n, cfg.model, "Batch API(50%)" if cfg.use_batch_api else "표준 API")
    log.info("예상 비용: $%s", est["BatchAPI_추정USD"] if cfg.use_batch_api
             else est["표준API_추정USD"])

    results, usage = llm.analyze_channels(sc, vf, industry, lift, cfg)
    # 기존 인사이트와 병합 — 부분 재실행을 지원한다
    merged = {**llm.load_insights(), **results}
    path = llm.save_insights(merged)
    errors = sum(1 for v in results.values() if "_error" in v)
    log.info("인사이트 %d건 저장 (오류 %d건) -> %s", len(results), errors, path)
    log.info("실제 사용량 — %s", usage.summary(batch=cfg.use_batch_api))


def cmd_report(args) -> None:
    vf, cm, sc = _load_analysis()
    lift_path = config.PROCESSED / "pattern_lift.csv"
    lift = pd.read_csv(lift_path, encoding="utf-8-sig") if lift_path.exists() else pd.DataFrame()
    industry = score.industry_summary(sc, vf)
    insights_df = llm.insights_to_frame(llm.load_insights())
    resolved = (pd.read_csv(config.RESOLVED_CSV, encoding="utf-8-sig")
                if config.RESOLVED_CSV.exists() else None)

    path = report_mod.build_report(sc, cm, vf, industry, lift, insights_df, resolved,
                                   path=Path(args.out) if args.out else config.REPORT_XLSX)
    log.info("리포트 생성 완료 -> %s (%.0f KB)", path, path.stat().st_size / 1024)
    if insights_df.empty:
        log.warning("LLM 인사이트가 비어 있습니다. `python -m ytbench llm` 실행 후 "
                    "report 를 다시 돌리면 03_LLM인사이트 시트가 채워집니다.")


def cmd_demo(args) -> None:
    """API 키 없이 전 과정을 실행해 구조를 검증한다."""
    from . import mock
    log.info("모의 데이터 생성 중 (경쟁사 %d개)...", args.companies_count)
    n_ch, n_vi = mock.generate(n_companies=args.companies_count)
    log.info("생성 완료 — 채널 %d, 영상 %d", n_ch, n_vi)
    mock.generate_companies_csv(args.companies_count)
    log.info("모의 경쟁사 목록 -> %s", config.COMPANIES_CSV)

    cmd_analyze(args)
    log.info("LLM 단계는 건너뜁니다 (키 필요). 03_LLM인사이트 시트는 안내문으로 채워집니다.")
    cmd_report(args)
    print(f"\n완료. 엑셀을 열어 구조를 확인하세요:\n  {config.REPORT_XLSX}\n")


def cmd_all(args) -> None:
    cmd_resolve(args)
    cmd_collect(args)
    cmd_analyze(args)
    if config.has_anthropic_key() and not args.skip_llm:
        cmd_llm(args)
    else:
        log.info("LLM 단계 건너뜀 (%s)",
                 "--skip-llm 지정" if args.skip_llm else
                 f"{config.ENV_ANTHROPIC_KEY} 없음")
    cmd_report(args)


def _common_parser() -> argparse.ArgumentParser:
    """모든 서브커맨드가 공유하는 옵션.

    서브커맨드마다 parents= 로 붙인다. 최상위 파서에만 두면
    `ytbench llm --max-channels 20` 처럼 서브커맨드 뒤에 쓸 수 없고,
    양쪽에 두면 서브파서 기본값이 앞서 지정한 값을 덮어쓴다.
    """
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("-v", "--verbose", action="store_true", help="디버그 로그 출력")
    c.add_argument("--companies", default=str(config.COMPANIES_CSV),
                   help="경쟁사 CSV 경로")
    c.add_argument("--companies-count", type=int, default=193,
                   help="demo/estimate 에서 가정할 경쟁사 수")
    c.add_argument("--max-videos", type=int, default=100,
                   help="채널당 수집할 최근 영상 수 (기본 100)")
    c.add_argument("--lookback-days", type=int, default=365,
                   help="이 일수 이내 업로드만 수집 (0=제한없음)")
    c.add_argument("--quota-budget", type=int, default=config.DAILY_QUOTA,
                   help="이번 실행에서 쓸 쿼터 상한")
    c.add_argument("--no-resume", action="store_true", help="이어받기 없이 처음부터 수집")
    c.add_argument("--thumbnails", action="store_true",
                   help="LLM 분석에 썸네일 이미지 포함 (비용 증가)")
    c.add_argument("--sync", action="store_true",
                   help="Batch API 대신 표준 API 사용 (즉시 결과, 2배 비용)")
    c.add_argument("--max-channels", type=int, default=None,
                   help="LLM 분석 채널 수 상한 (비용 제한용)")
    c.add_argument("--effort", default="medium",
                   choices=["low", "medium", "high", "xhigh", "max"],
                   help="LLM 추론 강도 (기본 medium)")
    c.add_argument("--skip-llm", action="store_true", help="all 실행 시 LLM 단계 생략")
    c.add_argument("--out", default=None, help="리포트 저장 경로")
    return c


def main(argv: list[str] | None = None) -> None:
    common = _common_parser()
    p = argparse.ArgumentParser(
        prog="python -m ytbench",
        description="에듀테크 경쟁사 유튜브 채널 자동 벤치마킹 파이프라인",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command", required=True)
    for name, fn, help_text in [
        ("estimate", cmd_estimate, "쿼터·비용 사전 추정 (API 호출 없음)"),
        ("resolve", cmd_resolve, "회사명 -> 유튜브 채널 매칭"),
        ("collect", cmd_collect, "채널 메타 + 최근 영상 수집"),
        ("analyze", cmd_analyze, "파생 지표 + 성공 패턴 + 벤치마크 스코어"),
        ("llm", cmd_llm, "채널별 배울 점/부족한 점 생성"),
        ("report", cmd_report, "엑셀 리포트 생성"),
        ("all", cmd_all, "resolve ~ report 일괄 실행"),
        ("demo", cmd_demo, "API 키 없이 전 과정 시연"),
    ]:
        sp = sub.add_parser(name, help=help_text, parents=[common])
        sp.set_defaults(func=fn)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    config.ensure_dirs()
    try:
        args.func(args)
    except KeyboardInterrupt:
        log.warning("사용자 중단 — 진행분은 저장되어 있습니다")
        sys.exit(130)
    except FileNotFoundError as exc:
        sys.exit(f"오류: {exc}")
    except RuntimeError as exc:
        # 설정 누락 등 사용자가 고칠 수 있는 오류 — 트레이스백을 숨긴다
        sys.exit(f"오류: {exc}")


if __name__ == "__main__":
    main()
