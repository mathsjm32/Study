"""YouTube refresh token 발급 (최초 1회).

두 가지 모드가 있다.

  1) 로컬 모드 — 채널 계정으로 이 PC에서 직접 로그인할 수 있을 때
         python scripts/get_youtube_token.py

     브라우저가 자동으로 열리고, 동의하면 토큰이 바로 출력된다.

  2) 원격 모드 — 채널 계정 소유자(담당자)가 자기 PC에서 동의해야 할 때
         python scripts/get_youtube_token.py --manual

     인증 URL이 출력된다. 이 URL을 담당자에게 전달하면 담당자가 본인 PC에서
     로그인·동의하고, 그 결과로 이동하는 주소를 회신해 준다. 그 주소를 이
     스크립트에 붙여넣으면 토큰 교환이 완료된다.
     담당자는 파이썬을 설치할 필요도, 이쪽 PC를 만질 필요도 없다.

주의: Google Cloud 콘솔의 OAuth 동의 화면이 '테스트' 상태이면 refresh token 이
7일 만에 만료된다. 반드시 '프로덕션(In production)' 으로 게시한 뒤 발급할 것.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import Flow, InstalledAppFlow  # noqa: E402

from config.settings import YT, require  # noqa: E402

SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]

# 루프백 리디렉션. 담당자 PC에는 서버가 없으므로 페이지는 열리지 않지만,
# 주소창에 인증 코드가 남는다. 코드 교환은 어느 PC에서 해도 무방하다.
MANUAL_REDIRECT_URI = "http://localhost:8080/"


def client_config() -> dict:
    require(YT_CLIENT_ID=YT.client_id, YT_CLIENT_SECRET=YT.client_secret)
    return {
        "installed": {
            "client_id": YT.client_id,
            "client_secret": YT.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def report(refresh_token: str | None) -> int:
    if not refresh_token:
        print("\nrefresh token 이 발급되지 않았습니다.")
        print("이미 승인한 적이 있는 계정이면 기존 권한을 해제한 뒤 다시 시도하세요:")
        print("  https://myaccount.google.com/permissions")
        return 1

    print("\n" + "=" * 64)
    print("발급 성공. 아래 줄을 .env 의 YT_REFRESH_TOKEN 에 넣으세요:\n")
    print(f"YT_REFRESH_TOKEN={refresh_token}")
    print("=" * 64)
    print("\n다음: python scripts/check_auth.py youtube")
    return 0


def run_local() -> int:
    """이 PC의 브라우저로 직접 로그인한다."""
    flow = InstalledAppFlow.from_client_config(client_config(), SCOPES)
    # access_type=offline + prompt=consent 여야 refresh token 이 내려온다.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent", open_browser=True
    )
    return report(creds.refresh_token)


def extract_code(pasted: str) -> str:
    """담당자가 회신한 주소(또는 코드)에서 인증 코드만 뽑아낸다."""
    pasted = pasted.strip().strip('"').strip("'")
    if pasted.startswith("http"):
        params = parse_qs(urlparse(pasted).query)
        if "error" in params:
            raise ValueError(f"담당자 화면에서 오류가 반환되었습니다: {params['error'][0]}")
        if "code" not in params:
            raise ValueError("주소에 code 파라미터가 없습니다. 전체 주소를 그대로 붙여넣어 주세요.")
        return params["code"][0]
    return pasted


def run_manual() -> int:
    """담당자가 다른 PC에서 동의하고, 그 결과만 받아 토큰으로 교환한다."""
    flow = Flow.from_client_config(
        client_config(), scopes=SCOPES, redirect_uri=MANUAL_REDIRECT_URI
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )

    print("=" * 64)
    print("1단계 — 아래 URL 을 채널 담당자에게 전달하세요.")
    print("=" * 64)
    print(f"\n{auth_url}\n")
    print("담당자가 할 일:")
    print("  1. 위 주소를 브라우저에 붙여넣기 (시크릿 창 권장)")
    print("  2. 채널 소유 계정으로 로그인")
    print("  3. '확인되지 않은 앱' 화면이 뜨면 → 고급 → (안전하지 않음) 이동")
    print("  4. 권한 허용")
    print("  5. '이 사이트에 연결할 수 없음' 페이지가 뜨는데 정상입니다.")
    print("     그 페이지의 주소창 전체를 복사해서 회신해 주세요.")
    print("     (http://localhost:8080/?code=... 형태)")
    print()
    print("※ 인증 코드는 약 10분 후 만료되니 회신받는 대로 진행하세요.")
    print("=" * 64)

    pasted = input("\n2단계 — 담당자가 회신한 주소를 붙여넣고 Enter: ")
    try:
        code = extract_code(pasted)
    except ValueError as exc:
        print(f"\n{exc}")
        return 1

    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        print(f"\n토큰 교환 실패: {exc}")
        print("  · 코드가 만료되었을 수 있습니다(10분). 스크립트를 다시 실행하세요.")
        print("  · 주소 전체가 아니라 일부만 붙여넣지 않았는지 확인하세요.")
        return 1

    return report(flow.credentials.refresh_token)


def main() -> int:
    if "--manual" in sys.argv:
        return run_manual()
    return run_local()


if __name__ == "__main__":
    raise SystemExit(main())
