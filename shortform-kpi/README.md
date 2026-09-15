# shortform-kpi

YouTube Shorts · Instagram Reels · Facebook Reels 성과 지표를 주기적으로 자동 수집해
BigQuery에 적재하고 Streamlit 대시보드로 추적하는 시스템.

```
수집(Python) → BigQuery(원본+정규화) → SQL 뷰(KPI) → Streamlit(대시보드)
       ↑
  GitHub Actions cron (하루 2회)
```

## 디렉터리

| 경로 | 역할 |
|---|---|
| `config/settings.py` | 환경 변수 로딩, 수집 지표 목록 |
| `sql/schema.sql` | BigQuery 테이블 DDL |
| `src/bq.py` | BigQuery 클라이언트 + 멱등 적재(UPSERT) |
| `src/collectors/` | 플랫폼별 수집기 (Step 1~3) |
| `scripts/` | 초기화·진단·토큰 발급 스크립트 |
| `app/` | Streamlit 대시보드 (Step 5) |

## 데이터 모델

| 테이블 | 내용 |
|---|---|
| `dim_account` | 계정 마스터 |
| `dim_content` | 콘텐츠 마스터(게시 후 변하지 않는 속성) |
| `fact_snapshot` | 수집 시점의 **누적** 지표 ← IG/FB 일별의 원천 |
| `fact_daily` | 콘텐츠×일자 **증분** 지표 |
| `fact_account_snapshot` | 팔로워 수 추이(도달률 계산용) |
| `ops_run_log` | 수집 실행 이력(결측 구간 탐지) |

**왜 스냅샷 테이블이 따로 있나?** YouTube Analytics API는 일별 데이터를 직접 주지만,
Instagram·Facebook은 **누적값만** 준다. 그래서 주기적으로 누적값을 찍어두고
차분(diff)해서 일별을 만든다.

---

# Step 0 — 자격증명 발급 (여기가 전부입니다)

이 시스템이 2개월 뒤 조용히 죽는 이유는 거의 항상 **토큰 만료**다. 아래 순서대로 하면 만료 없는 구성이 된다.

## A. BigQuery (GCP 서비스 계정)

1. [Google Cloud 콘솔](https://console.cloud.google.com/)에서 프로젝트 생성 (또는 기존 것 사용)
2. **API 및 서비스 → 라이브러리**에서 다음 3개 사용 설정
   - `BigQuery API`
   - `YouTube Data API v3`
   - `YouTube Analytics API`
3. **IAM 및 관리자 → 서비스 계정 → 만들기**
   - 역할: `BigQuery 데이터 편집자` + `BigQuery 작업 사용자`
4. 해당 서비스 계정 → **키 → 키 추가 → JSON** → 내려받아 `credentials/gcp-sa.json` 으로 저장
5. `.env`의 `GCP_PROJECT_ID` 에 프로젝트 ID 입력

> `credentials/` 와 `.env` 는 `.gitignore`에 등록되어 있다. 절대 커밋하지 말 것.

## B. YouTube (OAuth refresh token)

BigQuery와 달리 서비스 계정으로는 안 된다. **내 채널의 Analytics를 읽는 것이므로 채널 소유자 OAuth가 필요**하다.

1. **API 및 서비스 → OAuth 동의 화면**
   - 사용자 유형: 외부
   - ⚠️ **반드시 "게시(In production)" 상태로 전환**
     테스트 상태면 refresh token이 **7일 만에 만료**되어 매주 깨진다.
   - 범위에 `yt-analytics.readonly`, `youtube.readonly` 추가
2. **사용자 인증 정보 → OAuth 클라이언트 ID → 데스크톱 앱**
   → `YT_CLIENT_ID`, `YT_CLIENT_SECRET` 을 `.env`에 입력
3. 로컬 PC에서 실행 (브라우저가 열림):
   ```bash
   python scripts/get_youtube_token.py
   ```
   출력된 `YT_REFRESH_TOKEN` 을 `.env`에 붙여넣는다.

## C. Meta (Instagram + Facebook 공통)

Instagram Reels와 Facebook Reels는 **앱 하나·토큰 하나로** 둘 다 처리한다.

**사전 조건**
- Instagram 계정이 **비즈니스 또는 크리에이터** 계정일 것
- 그 Instagram 계정이 **Facebook 페이지에 연결**되어 있을 것
- 페이지와 IG 계정이 **비즈니스 관리자(Business Manager)** 자산으로 등록되어 있을 것

**절차**
1. [Meta 개발자 센터](https://developers.facebook.com/)에서 앱 생성 (유형: 비즈니스)
2. 앱에 **Instagram Graph API** / **Facebook 로그인** 제품 추가
3. [비즈니스 관리자](https://business.facebook.com/) → **설정 → 사용자 → 시스템 사용자 → 추가**
   - 역할: 관리자
   - **자산 할당**: 대상 Facebook 페이지 + Instagram 계정 + 위에서 만든 앱
4. 해당 시스템 사용자 → **새 토큰 생성**, 권한 선택:
   ```
   instagram_basic
   instagram_manage_insights
   pages_read_engagement
   pages_show_list
   read_insights
   business_management
   ```
   → 발급된 토큰을 `.env`의 `META_ACCESS_TOKEN` 에 입력

> **시스템 사용자 토큰이어야 만료가 없다.** 그래프 API 탐색기에서 뽑은 일반 토큰은
> 60일이면 죽는다. `check_auth.py` 가 만료 여부를 검사해 준다.

5. `IG_USER_ID` 와 `FB_PAGE_ID` 는 다음 단계의 진단 스크립트가 찾아서 알려준다.

---

## 실행

```bash
# 1) 환경 준비
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 값 채우기

# 2) BigQuery 데이터셋·테이블 생성
python scripts/init_bigquery.py

# 3) 자격증명 점검 (IG_USER_ID / FB_PAGE_ID 를 여기서 알려준다)
python scripts/check_auth.py

# 특정 항목만 점검
python scripts/check_auth.py meta
```

`check_auth.py` 가 세 항목 모두 "통과"면 Step 1로 넘어간다.

---

## ⚠️ 해석상 반드시 알아야 할 것

**플랫폼 간 조회수를 직접 비교하지 말 것.** 정의가 서로 다르다.

| 플랫폼 | 조회수 정의 |
|---|---|
| YouTube Shorts | 재생 시작 시점 카운트, **리플레이 포함** |
| Instagram | 재생 기준(2025년부터 impressions/plays를 `views`로 통합) |
| Facebook | 1ms 이상 재생, **리플레이 제외** |

비교는 ① 같은 플랫폼 내 시계열, ② 비율 지표(참여율·저장률·공유율)로만 한다.
대시보드(Step 5)에도 이 경고를 표시한다.

**Meta는 지표 이름을 자주 바꾼다.** 2025~2026년에 걸쳐 `impressions`→`views` 통합,
`reach`→viewer 계열 교체가 진행됐다. 그래서 수집기는 지표 이름을 하드코딩하지 않고
`config/settings.py` 의 후보 목록을 요청한 뒤 **거부된 지표만 빼고 재시도**하며,
원본 응답을 `raw_json` 에 보관해 나중에 재해석할 수 있게 한다.
