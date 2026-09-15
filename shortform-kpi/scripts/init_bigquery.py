"""BigQuery 데이터셋과 테이블을 생성한다 (몇 번 실행해도 안전).

    python scripts/init_bigquery.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import BQ  # noqa: E402
from src import bq  # noqa: E402


def main() -> int:
    print(f"프로젝트 : {BQ.project_id}")
    print(f"데이터셋 : {BQ.dataset}  (위치: {BQ.location})")
    print("-" * 56)

    try:
        ref = bq.ensure_dataset()
        print(f"[1/2] 데이터셋 준비 완료 → {ref}")
        tables = bq.ensure_tables()
        print(f"[2/2] 테이블 {len(tables)}개 준비 완료")
        for name in tables:
            print(f"        - {name}")
    except Exception as exc:
        print(f"\n실패: {exc}")
        print("\n확인할 것:")
        print("  · .env 의 GCP_PROJECT_ID 가 맞는지 (프로젝트 '번호'가 아니라 'ID')")
        print("  · 계정에 BigQuery 데이터 편집자 + 작업 사용자 역할이 있는지")
        print("  · 인증이 되어 있는지:")
        print("      - 키 파일 방식: GOOGLE_APPLICATION_CREDENTIALS 경로에 파일이 있는지")
        print("      - gcloud 방식 : gcloud auth application-default login 을 했는지")
        print("        (조직 정책으로 서비스 계정 키를 못 만들면 gcloud 방식을 쓴다)")
        return 1

    print("\n완료. 다음: python scripts/check_auth.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
