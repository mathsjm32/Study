"""자격증명 진단 — 발급한 토큰이 실제로 동작하는지 하나씩 확인한다.

    python scripts/check_auth.py             # 전체 점검
    python scripts/check_auth.py bigquery    # 하나만 점검

각 항목은 독립적이라, 아직 발급 안 한 플랫폼이 있어도 나머지는 검사된다.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from config.settings import BQ, META, YT, ConfigError  # noqa: E402

OK, FAIL, SKIP = "  [OK]  ", " [FAIL] ", " [SKIP] "


def _hr(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


# ---------------------------------------------------------------- BigQuery
def check_bigquery() -> bool:
    _hr("1. BigQuery")
    try:
        from src import bq

        client = bq.get_client()
        value = list(client.query("SELECT 1 AS ok").result())[0]["ok"]
        print(f"{OK}쿼리 실행 가능 (SELECT 1 → {value})")
        print(f"{OK}프로젝트: {BQ.project_id} / 위치: {BQ.location}")

        tables = bq.list_tables()
        if tables:
            print(f"{OK}데이터셋 '{BQ.dataset}' 테이블 {len(tables)}개: {', '.join(tables)}")
        else:
            print(f"{FAIL}데이터셋에 테이블이 없습니다 → python scripts/init_bigquery.py")
            return False
        return True
    except ConfigError as exc:
        print(f"{SKIP}{exc}")
        return False
    except Exception as exc:
        print(f"{FAIL}{type(exc).__name__}: {exc}")
        print("        · 서비스 계정 역할: BigQuery 데이터 편집자 + BigQuery 작업 사용자")
        return False


# ------------------------------------------------------------------ YouTube
def check_youtube() -> bool:
    _hr("2. YouTube (Analytics API)")
    try:
        YT.validate()
    except ConfigError as exc:
        print(f"{SKIP}{exc}")
        return False

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            None,
            refresh_token=YT.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=YT.client_id,
            client_secret=YT.client_secret,
        )
        creds.refresh(Request())
        print(f"{OK}refresh token 유효 — access token 갱신 성공")

        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        channels = youtube.channels().list(part="snippet,statistics", mine=True).execute()
        items = channels.get("items", [])
        if not items:
            print(f"{FAIL}이 계정에 연결된 채널이 없습니다")
            return False
        ch = items[0]
        print(f"{OK}채널: {ch['snippet']['title']} ({ch['id']})")
        print(f"        구독자 {ch['statistics'].get('subscriberCount', '?')}명 / "
              f"영상 {ch['statistics'].get('videoCount', '?')}개")
        if ch["id"] != YT.channel_id:
            print(f"{FAIL}.env 의 YT_CHANNEL_ID({YT.channel_id})가 다릅니다 → {ch['id']} 로 수정하세요")

        # Analytics API 가 실제로 숏츠를 구분해 주는지까지 확인한다.
        analytics = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
        end = dt.date.today() - dt.timedelta(days=3)
        start = end - dt.timedelta(days=27)
        report = analytics.reports().query(
            ids=f"channel=={ch['id']}",
            startDate=start.isoformat(),
            endDate=end.isoformat(),
            metrics="views,shares,videosAddedToPlaylists",
            dimensions="creatorContentType",
        ).execute()
        rows = report.get("rows", [])
        print(f"{OK}Analytics API 조회 성공 ({start} ~ {end})")
        if rows:
            for row in rows:
                print(f"        {row[0]:<20} 조회 {row[1]:>9,} / 공유 {row[2]:>7,} / 저장 {row[3]:>6,}")
            if not any(r[0] == "SHORTS" for r in rows):
                print("        (참고: 이 기간에 SHORTS 데이터가 없습니다)")
        else:
            print("        (참고: 이 기간 데이터가 없습니다 — 채널에 업로드가 있는지 확인)")
        return True
    except Exception as exc:
        print(f"{FAIL}{type(exc).__name__}: {exc}")
        print("        · OAuth 동의 화면이 '테스트' 상태면 refresh token 이 7일 만에 만료됩니다")
        print("        · 'In production' 으로 게시 후 재발급하세요")
        return False


# --------------------------------------------------------------------- Meta
def _graph(path: str, **params) -> dict:
    params["access_token"] = META.access_token
    resp = requests.get(f"{META.base_url}/{path}", params=params, timeout=30)
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"{err.get('type')}: {err.get('message')}")
    return data


def check_meta() -> bool:
    _hr("3. Meta (Instagram + Facebook)")
    if not META.access_token:
        print(f"{SKIP}META_ACCESS_TOKEN 이 비어 있습니다")
        return False

    ok = True
    try:
        me = _graph("me", fields="id,name")
        print(f"{OK}토큰 주체: {me.get('name', '(이름 없음)')} ({me['id']})")
    except Exception as exc:
        print(f"{FAIL}토큰이 유효하지 않습니다 — {exc}")
        return False

    # 만료 여부 — 자동화에서 가장 중요한 항목
    try:
        info = _graph("debug_token", input_token=META.access_token)["data"]
        expires = info.get("expires_at", 0)
        if expires == 0:
            print(f"{OK}만료 없음 (시스템 사용자 토큰) — 자동화에 적합")
        else:
            when = dt.datetime.fromtimestamp(expires, dt.timezone.utc)
            days = (when - dt.datetime.now(dt.timezone.utc)).days
            print(f"{FAIL}{when:%Y-%m-%d} 만료 (D-{days}) — 자동화가 그날 멈춥니다")
            print("        → 비즈니스 관리자에서 '시스템 사용자' 토큰으로 교체하세요")
            ok = False
        print(f"        권한: {', '.join(info.get('scopes', [])) or '(없음)'}")
    except Exception as exc:
        print(f"        (만료 확인 실패: {exc})")

    # 페이지 목록과 연결된 IG 계정 — .env 에 넣을 ID 를 여기서 알려준다
    try:
        accounts = _graph("me/accounts", fields="id,name,instagram_business_account{id,username}")
        pages = accounts.get("data", [])
        if pages:
            print(f"{OK}접근 가능한 페이지 {len(pages)}개:")
            for page in pages:
                ig = page.get("instagram_business_account")
                ig_text = f" ↔ IG @{ig['username']} (IG_USER_ID={ig['id']})" if ig else " (IG 연결 없음)"
                print(f"        FB_PAGE_ID={page['id']}  {page['name']}{ig_text}")
        else:
            print(f"{FAIL}접근 가능한 페이지가 없습니다 — 시스템 사용자에 페이지 자산을 할당했는지 확인")
            ok = False
    except Exception as exc:
        print(f"        (페이지 목록 조회 실패: {exc})")

    if META.ig_user_id:
        try:
            ig = _graph(META.ig_user_id, fields="username,followers_count,media_count")
            print(f"{OK}Instagram @{ig['username']} — 팔로워 {ig.get('followers_count', 0):,}명 / "
                  f"게시물 {ig.get('media_count', 0):,}개")
        except Exception as exc:
            print(f"{FAIL}IG_USER_ID 조회 실패 — {exc}")
            ok = False
    else:
        print(f"{SKIP}IG_USER_ID 미설정 (위 목록에서 복사해 .env 에 넣으세요)")

    if META.fb_page_id:
        try:
            page = _graph(META.fb_page_id, fields="name,fan_count")
            print(f"{OK}Facebook 페이지 '{page['name']}' — 팔로워 {page.get('fan_count', 0):,}명")
        except Exception as exc:
            print(f"{FAIL}FB_PAGE_ID 조회 실패 — {exc}")
            ok = False
    else:
        print(f"{SKIP}FB_PAGE_ID 미설정 (위 목록에서 복사해 .env 에 넣으세요)")

    return ok


CHECKS = {"bigquery": check_bigquery, "youtube": check_youtube, "meta": check_meta}


def main() -> int:
    targets = sys.argv[1:] or list(CHECKS)
    unknown = [t for t in targets if t not in CHECKS]
    if unknown:
        print(f"알 수 없는 대상: {', '.join(unknown)}  (가능: {', '.join(CHECKS)})")
        return 2

    results = {name: CHECKS[name]() for name in targets}

    _hr("결과 요약")
    for name, passed in results.items():
        print(f"  {name:<10} {'통과' if passed else '미완료'}")
    remaining = [n for n, p in results.items() if not p]
    if remaining:
        print(f"\n남은 작업: {', '.join(remaining)} — README.md 의 Step 0 절차를 참고하세요.")
        return 1
    print("\n모든 자격증명 준비 완료. Step 1(YouTube 수집기)로 넘어갈 수 있습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
