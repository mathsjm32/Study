"""BigQuery 클라이언트와 멱등 적재(UPSERT) 유틸리티.

모든 수집기는 여기의 `merge_rows()` 만 호출한다. 스테이징 테이블에 적재한
뒤 MERGE 문으로 대상 테이블에 반영하므로, 같은 데이터를 몇 번 넣어도
행이 중복되지 않는다.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence
from uuid import uuid4

from google.cloud import bigquery
from google.oauth2 import service_account

from config.settings import BQ, ROOT

SCHEMA_PATH = ROOT / "sql" / "schema.sql"
MIGRATIONS_PATH = ROOT / "sql" / "migrations.sql"


# --------------------------------------------------------------------------
# 클라이언트
# --------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_client() -> bigquery.Client:
    """서비스 계정 JSON(문자열 → 파일 → ADC) 순으로 자격증명을 찾는다."""
    BQ.validate()
    credentials = None

    if BQ.credentials_json:
        # GitHub Actions: Secret 에 JSON 전체를 넣어둔 경우
        info = json.loads(BQ.credentials_json)
        credentials = service_account.Credentials.from_service_account_info(info)
    elif BQ.credentials_path:
        path = Path(BQ.credentials_path)
        if not path.is_absolute():
            path = ROOT / path
        if path.exists():
            credentials = service_account.Credentials.from_service_account_file(str(path))

    # credentials 가 None 이면 google-auth 가 ADC(gcloud 로그인 등)를 찾는다.
    return bigquery.Client(
        project=BQ.project_id, credentials=credentials, location=BQ.location
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_key(*parts: Any) -> str:
    """대리 키 생성. 값이 길어져도 길이가 일정하도록 해시로 만든다."""
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 데이터셋 / 테이블 생성
# --------------------------------------------------------------------------
def ensure_dataset() -> str:
    """데이터셋이 없으면 만든다. 이미 있으면 그대로 둔다."""
    client = get_client()
    dataset = bigquery.Dataset(BQ.dataset_ref)
    dataset.location = BQ.location
    dataset.description = "숏폼(릴스/숏츠) KPI 추적 데이터셋"
    client.create_dataset(dataset, exists_ok=True)
    return BQ.dataset_ref


def ensure_tables() -> list[str]:
    """테이블을 만들고(schema.sql) 누락된 컬럼을 채운다(migrations.sql).

    둘 다 IF NOT EXISTS 라 이미 만들어진 데이터셋에 다시 돌려도 안전하다.
    """
    client = get_client()
    for path in (SCHEMA_PATH, MIGRATIONS_PATH):
        if not path.exists():
            continue
        sql = path.read_text(encoding="utf-8").replace("${DATASET}", BQ.dataset_ref)
        if sql.strip():
            client.query(sql).result()
    return list_tables()


def list_tables() -> list[str]:
    return sorted(t.table_id for t in get_client().list_tables(BQ.dataset_ref))


# --------------------------------------------------------------------------
# 멱등 적재
# --------------------------------------------------------------------------
def _dedupe(rows: Sequence[dict], key_cols: Sequence[str]) -> list[dict]:
    """같은 배치 안의 키 중복을 제거한다(뒤에 온 행이 이긴다).

    MERGE 는 소스에 중복 키가 있으면 에러를 내므로 반드시 필요하다.
    """
    seen: dict[tuple, dict] = {}
    for row in rows:
        seen[tuple(row.get(col) for col in key_cols)] = row
    return list(seen.values())


def merge_rows(
    table: str,
    rows: Iterable[dict],
    key_cols: Sequence[str],
    *,
    update: bool = True,
) -> int:
    """rows 를 table 에 UPSERT 하고 반영된 행 수를 돌려준다.

    Args:
        table: 테이블 이름 (예: "fact_daily")
        rows: 적재할 dict 목록. 대상 테이블에 없는 키는 무시된다.
        key_cols: 일치 판정에 쓸 컬럼들.
        update: False 면 기존 행을 건드리지 않고 신규만 INSERT 한다.
    """
    rows = list(rows)
    if not rows:
        return 0

    client = get_client()
    dest = BQ.table(table)
    dest_schema = client.get_table(dest).schema
    col_names = [f.name for f in dest_schema]

    # 대상 테이블에 존재하는 컬럼만 남긴다 (수집기가 여분의 키를 넣어도 안전).
    payload = [{k: v for k, v in row.items() if k in col_names} for row in rows]
    payload = _dedupe(payload, key_cols)

    staging = BQ.table(f"_stg_{table}_{uuid4().hex[:8]}")
    job_config = bigquery.LoadJobConfig(
        schema=dest_schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    client.load_table_from_json(payload, staging, job_config=job_config).result()

    try:
        on_clause = " AND ".join(f"T.{c} = S.{c}" for c in key_cols)
        insert_cols = ", ".join(col_names)
        insert_vals = ", ".join(f"S.{c}" for c in col_names)
        matched = ""
        if update:
            set_clause = ", ".join(
                f"T.{c} = S.{c}" for c in col_names if c not in key_cols
            )
            if set_clause:
                matched = f"WHEN MATCHED THEN UPDATE SET {set_clause}\n"

        merge_sql = (
            f"MERGE `{dest}` T\n"
            f"USING `{staging}` S\n"
            f"ON {on_clause}\n"
            f"{matched}"
            f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
        )
        job = client.query(merge_sql)
        job.result()
        return job.num_dml_affected_rows or 0
    finally:
        client.delete_table(staging, not_found_ok=True)


def query_df(sql: str, **params: Any):
    """SELECT 결과를 pandas DataFrame 으로. 파라미터는 @name 으로 참조."""
    job_config = None
    if params:
        job_config = bigquery.QueryJobConfig(
            query_parameters=[_as_param(k, v) for k, v in params.items()]
        )
    return get_client().query(sql, job_config=job_config).to_dataframe()


def _as_param(name: str, value: Any) -> bigquery.ScalarQueryParameter:
    type_map = {bool: "BOOL", int: "INT64", float: "FLOAT64"}
    return bigquery.ScalarQueryParameter(
        name, type_map.get(type(value), "STRING"), value
    )


# --------------------------------------------------------------------------
# 실행 로그
# --------------------------------------------------------------------------
def log_run(
    platform: str,
    started_at: datetime,
    status: str,
    rows_written: int = 0,
    message: str = "",
) -> None:
    """수집 실행 결과를 ops_run_log 에 남긴다. 실패해도 본 작업을 막지 않는다."""
    try:
        merge_rows(
            "ops_run_log",
            [
                {
                    "run_id": make_key(platform, started_at.isoformat()),
                    "platform": platform,
                    "started_at": started_at.isoformat(),
                    "finished_at": utcnow().isoformat(),
                    "run_date": started_at.date().isoformat(),
                    "status": status,
                    "rows_written": rows_written,
                    "message": message[:1000],
                }
            ],
            key_cols=["run_id"],
        )
    except Exception as exc:  # 로그 실패가 수집 실패가 되면 안 된다
        print(f"[warn] ops_run_log 기록 실패: {exc}")
