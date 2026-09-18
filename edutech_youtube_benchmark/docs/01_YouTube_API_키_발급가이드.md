# YouTube Data API v3 키 발급 가이드

소요 시간 약 5분, 비용 0원(신용카드 등록 불필요).

## 1. Google Cloud 프로젝트 생성

1. https://console.cloud.google.com 접속 (구글 계정 로그인)
2. 상단 프로젝트 선택 드롭다운 → **새 프로젝트**
3. 프로젝트 이름 입력 (예: `edutech-youtube-benchmark`) → **만들기**

## 2. YouTube Data API v3 사용 설정

1. 좌측 메뉴 **API 및 서비스 → 라이브러리**
2. 검색창에 `YouTube Data API v3` 입력 → 선택
3. **사용** 버튼 클릭

## 3. API 키 발급

1. 좌측 메뉴 **API 및 서비스 → 사용자 인증 정보**
2. 상단 **+ 사용자 인증 정보 만들기 → API 키**
3. 생성된 키를 복사 (`AIza...` 형태)

## 4. 키 제한 설정 (권장)

발급 직후 **키 제한** 을 눌러 아래를 설정하면 키가 유출돼도 피해가 제한됩니다.

- **애플리케이션 제한사항**: 없음 (서버 스크립트에서 호출하므로)
- **API 제한사항**: **키 제한** 선택 → `YouTube Data API v3` 만 체크

## 5. 환경변수 등록

키는 **절대 코드나 CSV에 적지 마세요.** 이 저장소의 `.gitignore`는 `.env`를
제외하지만, 소스에 하드코딩된 키는 막아주지 못합니다.

### Windows (PowerShell)

```powershell
# 현재 세션에만 적용
$env:YOUTUBE_API_KEY = "여기에_발급받은_키"

# 영구 적용 (새 터미널부터 유효)
[Environment]::SetEnvironmentVariable("YOUTUBE_API_KEY", "여기에_발급받은_키", "User")
```

### macOS / Linux

```bash
export YOUTUBE_API_KEY="여기에_발급받은_키"

# 영구 적용
echo 'export YOUTUBE_API_KEY="여기에_발급받은_키"' >> ~/.zshrc
```

### 확인

```bash
python -m ytbench estimate
```

키가 없으면 `estimate`는 그대로 동작하지만 `resolve`/`collect`는 안내와 함께 중단됩니다.

## 6. 쿼터 이해하기 — 이 프로젝트의 핵심 제약

무료 기본 할당량은 **하루 10,000 units**이고, 태평양시 자정(한국시간 오후 4~5시)에 리셋됩니다.

| 호출 | 비용 | 193개 경쟁사 기준 |
|---|---|---|
| `search.list` (회사명으로 채널 찾기) | **100 units** | 19,300 units ← 전체의 96% |
| `channels.list` (채널 상세) | 1 unit | 4 units (50개씩 묶음) |
| `playlistItems.list` (업로드 목록) | 1 unit | 386 units |
| `videos.list` (영상 상세) | 1 unit | 386 units |

즉 **비싼 것은 '회사명으로 채널을 찾는 단계' 하나뿐**입니다. 영상 수집 자체는
193채널 × 100영상을 다 긁어도 800 units 미만이라 매일 갱신해도 부담이 없습니다.

### 실무 권장 순서

1. `companies.csv`에 **아는 채널 URL은 미리 채워 넣습니다.**
   URL 1건을 채울 때마다 100 units → 1 unit으로 줄어듭니다.
   193개 전부 채우면 전체 수집이 **하루 안에, 쿼터의 8%만 써서** 끝납니다.
2. URL을 모르는 회사만 자동 검색에 맡깁니다.
   쿼터가 소진되면 파이프라인이 진행분을 저장하고 멈추며, 다음 날 재실행하면
   이미 검색한 회사는 **디스크 캐시에서 0 units로** 건너뜁니다.
3. 쿼터 증액이 필요하면 Google Cloud Console에서
   **API 및 서비스 → YouTube Data API v3 → 할당량 → 할당량 상향 요청**
   을 신청할 수 있습니다(심사 필요, 수일 소요).

## 7. 자주 만나는 오류

| 메시지 | 원인 | 해결 |
|---|---|---|
| `403 quotaExceeded` | 일일 쿼터 소진 | 다음 날 재실행 (이어받기 자동) |
| `403 Method doesn't allow unregistered callers` | 키가 전달되지 않음 | 환경변수 이름이 `YOUTUBE_API_KEY`인지 확인 |
| `403 accessNotConfigured` | API 사용 설정 누락 | 2단계 다시 수행 |
| `400 API key not valid` | 키 오타 / 제한 설정 오류 | 4단계의 API 제한사항 확인 |
| `404 channelNotFound` | 채널 삭제·비공개 | 정상 — 리포트에 '실패'로 기록됨 |
