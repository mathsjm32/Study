"""4단계: LLM 정성 분석 — '배울 점 / 부족한 점' 자동 생성.

지표만으로는 "참여율 백분위 23"까지만 알 수 있고, "왜 낮은지, 무엇을
바꿔야 하는지"는 알 수 없다. 이 단계가 그 간극을 메운다.

프롬프트 설계 원칙:
  · 근거 없는 조언을 막기 위해, 채널별 실제 수치와 상·하위 영상 제목을
    전부 넣고 "제시된 데이터에 근거해서만" 판단하게 제약한다.
  · 업계 기준선과 성공 패턴 리프트 표를 system 프롬프트에 고정해
    프롬프트 캐싱이 걸리게 한다 (193건 반복 호출 비용의 핵심 절감).
  · 출력은 JSON 스키마로 강제해 엑셀 리포트에 바로 꽂는다.

비용: Batch API(기본 활성)를 쓰면 표준 대비 50%. 193채널 기준
입력이 대부분 캐시 히트여서 실제 청구액은 추정치보다 더 낮게 나온다.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

from . import config

log = logging.getLogger(__name__)

# 엑셀 리포트 컬럼과 1:1 대응하는 출력 스키마
INSIGHT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "positioning": {
            "type": "string",
            "description": "이 채널의 콘텐츠 포지셔닝을 한 문장으로",
        },
        "content_formula": {
            "type": "string",
            "description": "이 채널이 반복하는 성공 공식(또는 실패 패턴) 한 문장",
        },
        "threat_level": {"type": "string", "enum": ["높음", "중간", "낮음"]},
        "learnings": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string", "description": "배울 점 (간결하게)"},
                    "evidence": {
                        "type": "string",
                        "description": "제시된 수치·제목 중 근거가 된 것을 구체적으로 인용",
                    },
                    "applicability": {"type": "string", "enum": ["높음", "중간", "낮음"]},
                },
                "required": ["point", "evidence", "applicability"],
                "additionalProperties": False,
            },
        },
        "gaps": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string", "description": "부족한 점 / 빈틈"},
                    "evidence": {"type": "string", "description": "근거 수치 인용"},
                    "opportunity": {
                        "type": "string",
                        "description": "이 빈틈을 우리가 파고들 수 있는 방법",
                    },
                },
                "required": ["point", "evidence", "opportunity"],
                "additionalProperties": False,
            },
        },
        "actions": {
            "type": "array",
            "minItems": 2,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "우리 채널이 당장 실행할 것"},
                    "rationale": {"type": "string"},
                    "effort": {"type": "string", "enum": ["낮음", "중간", "높음"]},
                },
                "required": ["action", "rationale", "effort"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["positioning", "content_formula", "threat_level",
                 "learnings", "gaps", "actions"],
    "additionalProperties": False,
}

SYSTEM_INSTRUCTIONS = """\
당신은 에듀테크 기업의 유튜브 콘텐츠 전략을 분석하는 시니어 콘텐츠 전략가입니다.
경쟁사 채널 1개의 실제 수집 데이터를 받아, 우리 회사 SNS 콘텐츠 활성화에 쓸
벤치마킹 분석을 작성합니다.

절대 규칙:
1. 제시된 수치와 영상 제목에 근거해서만 판단합니다. 데이터에 없는 사실
   (매출, 조직 규모, 광고비, 촬영 장비 등)을 추측해 쓰지 않습니다.
2. 모든 '배울 점'과 '부족한 점'에는 근거로 삼은 수치나 제목을 그대로 인용합니다.
   예: "참여율 4.1%(업계 상위 12%)", "제목 '[수능 국어] 3개월 만에 1등급' 등 성과인증형".
3. 조회수 절대값이 아니라 백분위와 구독자당 지표로 판단합니다. 규모가 크다는
   이유만으로 '배울 점'이 되지 않습니다.
4. '부족한 점'은 비난이 아니라 우리가 파고들 빈틈(opportunity)으로 서술합니다.
5. 실행안(actions)은 우리 채널이 2주 안에 착수할 수 있는 구체적 행동으로 씁니다.
   "콘텐츠 품질 향상" 같은 추상적 표현은 금지합니다.
6. 모든 출력은 한국어로 작성합니다.
"""


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0

    def add(self, usage: Any) -> None:
        self.requests += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def cost_usd(self, batch: bool = False) -> float:
        """Claude Opus 5 기준 추정 비용 (input $5 / output $25 per MTok)."""
        rate = 0.5 if batch else 1.0
        return round(
            (self.input_tokens * 5.0
             + self.cache_write_tokens * 6.25       # 캐시 쓰기 = 입력의 1.25배
             + self.cache_read_tokens * 0.5         # 캐시 읽기 = 입력의 0.1배
             + self.output_tokens * 25.0) / 1_000_000 * rate,
            4,
        )

    def summary(self, batch: bool = False) -> str:
        return (f"{self.requests}건 · 입력 {self.input_tokens:,} "
                f"(캐시읽기 {self.cache_read_tokens:,}) · 출력 {self.output_tokens:,} "
                f"· 추정 ${self.cost_usd(batch)}")


# ── 프롬프트 조립 ─────────────────────────────────────────────────────
def build_system_blocks(
    industry: pd.DataFrame, lift_table: pd.DataFrame
) -> list[dict[str, Any]]:
    """193건 호출에서 동일하게 재사용되는 system 프롬프트 (캐싱 대상).

    마지막 블록에만 cache_control을 달면 그 앞 전체가 캐시 접두사가 된다.
    """
    baseline = industry.to_string(index=False) if not industry.empty else "(집계 없음)"
    patterns = (lift_table.head(15).to_string(index=False)
                if not lift_table.empty else "(표본 부족으로 미산출)")

    context = f"""\
## 업계 기준선 (수집된 에듀테크 경쟁사 전체)
{baseline}

## 제목·포맷 패턴별 성과 리프트
리프트 = (패턴 적용 영상의 채널내 상대조회수 중위) / (미적용 영상의 중위).
1.0 초과면 해당 패턴이 실제로 성과에 기여한다는 뜻입니다.
{patterns}

위 두 표가 '업계 평균'의 정의입니다. 개별 채널을 평가할 때 이 기준과 비교하세요.
"""
    return [
        {"type": "text", "text": SYSTEM_INSTRUCTIONS},
        {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
    ]


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    """수치를 천단위 구분해 표기한다.

    DataFrame을 거치면 파이썬 int가 np.int64가 되어 isinstance(x, int)가
    False가 된다. numbers 추상 타입으로 판정해 구분자 누락을 막는다.
    """
    import numbers
    if value is None or value == "":
        return "N/A"
    if isinstance(value, numbers.Real) and pd.isna(value):
        return "N/A"
    if isinstance(value, numbers.Integral):
        return f"{int(value):,}{suffix}"
    if isinstance(value, numbers.Real):
        return f"{float(value):,.{digits}f}{suffix}"
    return f"{value}{suffix}"


def build_channel_prompt(
    row: pd.Series, videos: pd.DataFrame, cfg: config.LLMConfig
) -> list[dict[str, Any]]:
    """채널 1개의 분석 요청 본문 (텍스트 + 선택적 썸네일 이미지)."""
    g = videos[videos["channel_id"] == row["channel_id"]]

    def video_lines(frame: pd.DataFrame) -> str:
        if frame.empty:
            return "(없음)"
        out = []
        for _, v in frame.iterrows():
            out.append(
                f"- [{v['format']}/{int(v['duration_sec'])}초] {v['title']}\n"
                f"  조회 {int(v['view_count']):,} (채널중위 대비 "
                f"{_fmt(v['views_vs_channel_median'])}배) · "
                f"참여율 {_fmt(v['engagement_rate'] * 100 if pd.notna(v['engagement_rate']) else None)}% · "
                f"{v['upload_weekday']}요일 {int(v['upload_hour']) if pd.notna(v['upload_hour']) else '?'}시"
            )
        return "\n".join(out)

    top = g.nlargest(cfg.top_n_videos, "view_count")
    bottom = g.nsmallest(cfg.bottom_n_videos, "view_count")

    scores = " · ".join(
        f"{axis} {_fmt(row.get(f'점수_{axis}'), 0)}" for axis in config.SCORE_WEIGHTS
    )

    text = f"""\
# 분석 대상: {row['company_name']} ({row.get('channel_title', '')})
업계 종합순위 {row.get('업계순위', '?')}위 / {row.get('등급', '?')} · 종합점수 {_fmt(row.get('종합점수'), 1)}
6축 백분위: {scores}
세그먼트: {row.get('category') or '미분류'}

## 채널 규모
- 구독자 {_fmt(int(row['subscriber_count']))}명 · 총 조회수 {_fmt(int(row['channel_view_count']))}회 · 전체 영상 {_fmt(int(row['channel_video_count']))}개
- 구독자당 누적 조회수 {_fmt(row.get('views_per_subscriber'))}배
- 분석 표본: 최근 {int(row.get('videos_sampled', 0))}개 영상

## 성과 분포
- 영상당 조회수: 중위 {_fmt(row.get('median_views'), 0)} / 상위10% {_fmt(row.get('p90_views'), 0)} / 최대 {_fmt(row.get('max_views'), 0)}
- 조회수 편차(CV) {_fmt(row.get('views_dispersion_cv'))} · 히트영상 비율 {_fmt((row.get('hit_ratio') or 0) * 100, 1)}%
- 참여율 중위 {_fmt((row.get('engagement_rate_median') or 0) * 100, 2)}% (좋아요 {_fmt((row.get('like_rate_median') or 0) * 100, 2)}% / 댓글 {_fmt((row.get('comment_rate_median') or 0) * 100, 3)}%)
- 90일 모멘텀 {_fmt(row.get('momentum_90d'))} (1.0 초과=성장)

## 업로드 운영
- 최근 90일 {int(row.get('uploads_last_90d', 0))}개 · 최근 30일 {int(row.get('uploads_last_30d', 0))}개
- 업로드 간격 중위 {_fmt(row.get('upload_interval_median_days'), 1)}일 · 규칙성 CV {_fmt(row.get('upload_regularity_cv'))} (낮을수록 규칙적)
- 마지막 업로드 {_fmt(row.get('days_since_last_upload'), 0)}일 전
- 최다 업로드: {row.get('최다_업로드요일', '?')}요일 {row.get('최다_업로드시간', '?')}시

## 콘텐츠 포맷 믹스
- 숏폼 {_fmt((row.get('숏폼_비율') or 0) * 100, 1)}% (중위조회 {_fmt(row.get('숏폼_중위조회수'), 0)}) · 미들폼 {_fmt((row.get('미들폼_비율') or 0) * 100, 1)}% (중위조회 {_fmt(row.get('미들폼_중위조회수'), 0)}) · 롱폼 {_fmt((row.get('롱폼_비율') or 0) * 100, 1)}% (중위조회 {_fmt(row.get('롱폼_중위조회수'), 0)})
- 영상 길이 중위 {_fmt(row.get('duration_median_sec'), 0)}초 · 제목 길이 중위 {_fmt(row.get('title_len_median'), 0)}자 · 태그 사용률 {_fmt((row.get('태그_사용률') or 0) * 100, 1)}%

## 상위 조회수 영상 {len(top)}개
{video_lines(top)}

## 하위 조회수 영상 {len(bottom)}개
{video_lines(bottom)}

위 데이터만 근거로, 이 채널에서 우리가 배울 점과 부족한 점, 그리고 우리 채널 실행안을 작성하세요."""

    content: list[dict[str, Any]] = []
    if cfg.analyze_thumbnails:
        urls = [u for u in top["thumbnail_url"].head(cfg.thumbnail_sample).tolist() if u]
        for url in urls:
            content.append({"type": "image", "source": {"type": "url", "url": url}})
        if urls:
            content.append({
                "type": "text",
                "text": f"위 이미지 {len(urls)}장은 이 채널 상위 조회수 영상의 썸네일입니다. "
                        "인물 등장 여부, 텍스트 분량, 색상 대비, 레이아웃 유형을 "
                        "배울 점/부족한 점 근거에 반영하세요.",
            })
    content.append({"type": "text", "text": text})
    return content


# ── 실행 ──────────────────────────────────────────────────────────────
def _request_params(system: list[dict], content: list[dict], cfg: config.LLMConfig) -> dict:
    return {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "output_config": {
            "effort": cfg.effort,
            "format": {"type": "json_schema", "schema": INSIGHT_SCHEMA},
        },
    }


def _extract(message: Any) -> dict[str, Any]:
    """JSON 스키마 강제 출력에서 결과를 뽑는다. refusal은 명시적으로 표시."""
    if getattr(message, "stop_reason", None) == "refusal":
        detail = getattr(message, "stop_details", None)
        return {"_error": f"모델이 응답을 거부했습니다 (category={getattr(detail, 'category', '?')})"}
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_error": f"JSON 파싱 실패: {text[:300]}"}


def analyze_channels(
    scores: pd.DataFrame,
    video_features: pd.DataFrame,
    industry: pd.DataFrame,
    lift_table: pd.DataFrame,
    cfg: config.LLMConfig | None = None,
) -> tuple[dict[str, dict[str, Any]], LLMUsage]:
    """채널별 정성 인사이트를 생성한다.

    Returns: ({company_name: 인사이트}, 사용량)
    """
    import anthropic

    cfg = cfg or config.LLMConfig()
    if not config.has_anthropic_key():
        raise RuntimeError(
            f"{config.ENV_ANTHROPIC_KEY} 환경변수가 없습니다.\n"
            "  발급: https://console.anthropic.com -> API Keys\n"
            '  설정(Windows): $env:ANTHROPIC_API_KEY = "sk-ant-..."\n'
            '  설정(mac/Linux): export ANTHROPIC_API_KEY="sk-ant-..."\n'
            "  LLM 없이 지표만 쓰려면 이 단계를 건너뛰고 "
            "`python -m ytbench report` 로 진행하세요 "
            "(all 실행 시에는 --skip-llm)."
        )

    targets = scores if cfg.max_channels is None else scores.head(cfg.max_channels)
    targets = targets[targets["videos_sampled"] > 0]
    if targets.empty:
        log.warning("영상이 수집된 채널이 없어 LLM 분석을 건너뜁니다")
        return {}, LLMUsage()

    client = anthropic.Anthropic()
    system = build_system_blocks(industry, lift_table)
    usage = LLMUsage()
    results: dict[str, dict[str, Any]] = {}

    if cfg.use_batch_api:
        return _run_batch(client, targets, video_features, system, cfg)

    for i, (_, row) in enumerate(targets.iterrows(), 1):
        content = build_channel_prompt(row, video_features, cfg)
        try:
            msg = client.messages.create(**_request_params(system, content, cfg))
        except anthropic.RateLimitError as exc:
            log.warning("[%d/%d] 레이트리밋 — 30초 후 재시도: %s", i, len(targets), exc)
            time.sleep(30)
            msg = client.messages.create(**_request_params(system, content, cfg))
        except anthropic.APIStatusError as exc:
            log.error("[%d/%d] %s 분석 실패: %s", i, len(targets), row["company_name"], exc)
            results[row["company_name"]] = {"_error": str(exc)}
            continue

        usage.add(msg.usage)
        results[row["company_name"]] = _extract(msg)
        log.info("[%d/%d] %s 완료 (%s)", i, len(targets), row["company_name"],
                 usage.summary())

    return results, usage


def _run_batch(client, targets, video_features, system, cfg):
    """Batch API 경로 — 표준 대비 50% 비용, 최대 24시간."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    requests_payload = []
    id_to_company: dict[str, str] = {}
    for idx, (_, row) in enumerate(targets.iterrows()):
        custom_id = f"ch-{idx:04d}"
        id_to_company[custom_id] = row["company_name"]
        content = build_channel_prompt(row, video_features, cfg)
        requests_payload.append(Request(
            custom_id=custom_id,
            params=MessageCreateParamsNonStreaming(
                **_request_params(system, content, cfg)
            ),
        ))

    batch = client.messages.batches.create(requests=requests_payload)
    log.info("Batch 생성 — id=%s, 요청 %d건. 완료까지 보통 1시간 이내입니다.",
             batch.id, len(requests_payload))

    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        counts = batch.request_counts
        log.info("Batch 진행 중 — 처리중 %d / 성공 %d / 실패 %d",
                 counts.processing, counts.succeeded, counts.errored)
        time.sleep(60)

    usage = LLMUsage()
    results: dict[str, dict[str, Any]] = {}
    # 결과 순서는 보장되지 않으므로 custom_id로 매핑한다
    for result in client.messages.batches.results(batch.id):
        company = id_to_company.get(result.custom_id, result.custom_id)
        kind = result.result.type
        if kind == "succeeded":
            msg = result.result.message
            usage.add(msg.usage)
            results[company] = _extract(msg)
        else:
            reason = getattr(getattr(result.result, "error", None), "type", kind)
            log.warning("%s 실패(%s)", company, reason)
            results[company] = {"_error": f"batch {kind}: {reason}"}

    log.info("Batch 완료 — %s", usage.summary(batch=True))
    return results, usage


def estimate_cost(n_channels: int, cfg: config.LLMConfig | None = None) -> dict[str, Any]:
    """실행 전 비용 추정. 실측 토큰 기준의 보수적 어림값."""
    cfg = cfg or config.LLMConfig()
    per_input = 2_600 + (1_600 * cfg.thumbnail_sample if cfg.analyze_thumbnails else 0)
    per_output = 1_100
    cached_share = 0.55         # system 블록이 차지하는 비율(캐시 히트 대상)

    fresh_in = per_input * (1 - cached_share) * n_channels
    cached_in = per_input * cached_share * n_channels
    out = per_output * n_channels
    std = (fresh_in * 5 + cached_in * 0.5 + out * 25) / 1_000_000
    return {
        "채널수": n_channels,
        "썸네일분석": cfg.analyze_thumbnails,
        "예상_입력토큰": int(fresh_in + cached_in),
        "예상_출력토큰": int(out),
        "표준API_추정USD": round(std, 2),
        "BatchAPI_추정USD": round(std * 0.5, 2),
    }


def save_insights(results: dict[str, Any], path=config.INSIGHTS_JSON):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_insights(path=config.INSIGHTS_JSON) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def insights_to_frame(results: dict[str, Any]) -> pd.DataFrame:
    """인사이트 dict -> 엑셀용 평탄 테이블."""
    rows = []
    for company, data in results.items():
        if "_error" in data:
            rows.append({"company_name": company, "오류": data["_error"]})
            continue
        base = {
            "company_name": company,
            "포지셔닝": data.get("positioning", ""),
            "성공공식": data.get("content_formula", ""),
            "위협도": data.get("threat_level", ""),
        }
        for i, item in enumerate(data.get("learnings", []), 1):
            base[f"배울점{i}"] = item.get("point", "")
            base[f"배울점{i}_근거"] = item.get("evidence", "")
            base[f"배울점{i}_적용성"] = item.get("applicability", "")
        for i, item in enumerate(data.get("gaps", []), 1):
            base[f"부족한점{i}"] = item.get("point", "")
            base[f"부족한점{i}_근거"] = item.get("evidence", "")
            base[f"부족한점{i}_기회"] = item.get("opportunity", "")
        for i, item in enumerate(data.get("actions", []), 1):
            base[f"실행안{i}"] = item.get("action", "")
            base[f"실행안{i}_이유"] = item.get("rationale", "")
            base[f"실행안{i}_난이도"] = item.get("effort", "")
        rows.append(base)
    return pd.DataFrame(rows)
