"""Meta Graph API 공용 클라이언트 (Instagram + Facebook 공유).

Meta 는 지표 이름을 자주 바꾼다(2025~2026 년에 impressions → views 통합,
reach → viewer 계열 교체). 그래서 이 모듈은 두 가지를 제공한다.

  * `MetaError`      — 오류 코드·메시지를 구조화해 호출부가 판단할 수 있게 한다
  * `resolve_metrics` — 후보 지표를 실제로 한 번 호출해 보고, 거부되는 것만
                        빼서 '이 계정에서 실제로 되는 지표 목록'을 확정한다

지표 확정은 실행당 한 번만 한다. 미디어마다 재시도하면 호출량이 폭증한다.
"""
from __future__ import annotations

import time
from typing import Any, Iterator, Sequence

import requests

from config.settings import META

# 일시적 실패로 보고 재시도할 오류 코드
# 4: 앱 단위 호출 한도, 17: 사용자 단위 한도, 32: 페이지 한도,
# 613: 호출 빈도 초과, 1/2: 알 수 없는 일시 오류
RETRYABLE_CODES = {1, 2, 4, 17, 32, 613}
MAX_RETRIES = 4


class MetaError(RuntimeError):
    """Graph API 가 error 객체를 돌려줬을 때."""

    def __init__(self, payload: dict):
        error = payload.get("error", {})
        self.code = error.get("code")
        self.subcode = error.get("error_subcode")
        self.message = error.get("message", "")
        self.type = error.get("type", "")
        super().__init__(f"({self.code}) {self.message}")

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


def get(path: str, **params: Any) -> dict:
    """Graph API GET. 한도 초과는 지수 백오프로 재시도한다."""
    params["access_token"] = META.access_token
    url = f"{META.base_url}/{path.lstrip('/')}"

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, params=params, timeout=60)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"Graph API 호출 실패: {exc}") from exc
            time.sleep(2 ** attempt)
            continue

        if "error" not in payload:
            return payload

        error = MetaError(payload)
        if not error.retryable or attempt == MAX_RETRIES - 1:
            raise error
        wait = 2 ** (attempt + 1)
        print(f"    [retry] {error} — {wait}초 후 재시도")
        time.sleep(wait)

    raise RuntimeError("재시도 한도 초과")  # 도달하지 않음


def paginate(path: str, *, limit: int = 100, max_pages: int = 100, **params: Any) -> Iterator[dict]:
    """엣지를 페이지 단위로 순회하며 항목을 하나씩 내보낸다."""
    params["limit"] = limit
    page = get(path, **params)
    for _ in range(max_pages):
        yield from page.get("data", [])
        next_url = (page.get("paging") or {}).get("next")
        if not next_url:
            return
        response = requests.get(next_url, timeout=60)
        page = response.json()
        if "error" in page:
            raise MetaError(page)


def insight_values(payload: dict) -> dict[str, Any]:
    """인사이트 응답에서 {지표명: 값} 만 뽑아낸다.

    Meta 는 지표에 따라 values[0].value 또는 total_value.value 로 값을 준다.
    """
    values: dict[str, Any] = {}
    for item in payload.get("data", []):
        name = item.get("name")
        if not name:
            continue
        entries = item.get("values") or []
        if entries and "value" in entries[0]:
            values[name] = entries[0]["value"]
        elif isinstance(item.get("total_value"), dict):
            values[name] = item["total_value"].get("value")
    return values


def resolve(
    path: str,
    candidates: Sequence[str],
    *,
    param: str = "metric",
    label: str = "",
    **params: Any,
) -> list[str]:
    """후보 중 이 계정/노드에서 실제로 조회되는 것만 남긴다.

    지표(`metric`)와 필드(`fields`) 모두에 쓴다. Meta 는 거부한 이름을 오류
    메시지에 그대로 담아 주므로("Tried accessing nonexisting field (x)"),
    메시지에서 범인을 찾아 빼고 다시 시도한다. 특정하지 못하면 하나씩
    확인한다. 실행당 한 번만 하므로 호출량이 늘지 않는다.
    """
    kind = "지표" if param == "metric" else "필드"
    working = list(candidates)

    while working:
        try:
            get(path, **{param: ",".join(working)}, **params)
            return working
        except MetaError as error:
            rejected = [name for name in working if name in error.message]
            if not rejected:
                break
            for name in rejected:
                print(f"    [{kind} 제외] {name} — API 가 거부함")
            working = [name for name in working if name not in rejected]

    print(f"    {kind}를 하나씩 확인합니다{f' ({label})' if label else ''}…")
    survivors: list[str] = []
    for name in candidates:
        try:
            get(path, **{param: name}, **params)
            survivors.append(name)
        except MetaError as error:
            print(f"    [{kind} 제외] {name} — {error.message[:80]}")
    return survivors


def resolve_metrics(path: str, candidates: Sequence[str], **kwargs: Any) -> list[str]:
    return resolve(path, candidates, param="metric", **kwargs)


def resolve_fields(path: str, candidates: Sequence[str], **kwargs: Any) -> list[str]:
    return resolve(path, candidates, param="fields", **kwargs)


def apply_map(
    values: dict[str, Any], mapping: dict[str, str], convert=None
) -> dict[str, Any]:
    """{지표명: 값} 을 {컬럼: 값} 으로 옮긴다.

    같은 컬럼에 여러 지표가 매핑될 수 있다(플랫폼이 이름을 바꾼 경우 등).
    mapping 에 먼저 적힌 지표가 우선하고, 값이 없으면 다음 후보로 넘어간다.
    """
    out: dict[str, Any] = {}
    for metric, column in mapping.items():
        if out.get(column) is not None:
            continue
        value = values.get(metric)
        if value is None:
            continue
        out[column] = convert(value) if convert else value
    return out
