"""YouTube refresh token 발급 (최초 1회, 로컬 PC에서 실행).

    python scripts/get_youtube_token.py

브라우저가 열리면 데이터를 수집할 채널의 구글 계정으로 로그인한다.
출력된 refresh token 을 .env 의 YT_REFRESH_TOKEN 에 붙여넣으면 된다.

주의: Google Cloud 콘솔의 OAuth 동의 화면이 '테스트' 상태이면 refresh token 이
7일 만에 만료된다. 반드시 '프로덕션(In production)' 으로 게시한 뒤 발급할 것.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from config.settings import YT, require  # noqa: E402

SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def main() -> int:
    require(YT_CLIENT_ID=YT.client_id, YT_CLIENT_SECRET=YT.client_secret)

    client_config = {
        "installed": {
            "client_id": YT.client_id,
            "client_secret": YT.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # access_type=offline + prompt=consent 여야 refresh token 이 내려온다.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent", open_browser=True
    )

    if not creds.refresh_token:
        print("refresh token 이 발급되지 않았습니다.")
        print("구글 계정의 기존 앱 권한을 해제한 뒤 다시 시도하세요:")
        print("  https://myaccount.google.com/permissions")
        return 1

    print("\n" + "=" * 60)
    print("발급 성공. 아래 줄을 .env 에 넣으세요:\n")
    print(f"YT_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
