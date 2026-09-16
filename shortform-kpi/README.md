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

## A. BigQuery

1. [Google Cloud 콘솔](https://console.cloud.google.com/)에서 프로젝트 생성 (또는 기존 것 사용)
2. `.env` 의 `GCP_PROJECT_ID` 에 **프로젝트 ID** 입력 (프로젝트 '번호'가 아니다)
3. **API 및 서비스 → 라이브러리**에서 다음 3개 사용 설정
   - `BigQuery API`
   - `YouTube Data API v3`
   - `YouTube Analytics API` ← 조회수·공유·저장 지표의 출처. 빠뜨리기 쉬움
4. 아래 두 방식 중 하나로 인증

### 방식 A — 서비스 계정 키 파일

1. **IAM 및 관리자 → 서비스 계정 → 서비스 계정 만들기**
   - 이름 `shortform-collector`
   - 역할: `BigQuery 데이터 편집자` + `BigQuery 작업 사용자`
2. 생성된 계정 → **키** 탭 → **키 추가 → 새 키 만들기 → JSON**
3. 내려받은 파일을 `credentials/gcp-sa.json` 으로 저장
4. `.env` 에 경로 입력:
   ```bash
   GOOGLE_APPLICATION_CREDENTIALS=./credentials/gcp-sa.json
   ```

### 방식 B — gcloud 로그인 (권장)

조직에 `iam.disableServiceAccountKeyCreation` 정책이 걸려 있으면 방식 A의 3번에서
키 생성이 차단된다. 그때는 이쪽을 쓴다. 키 파일을 만들지 않으므로 유출 위험이 없어
로컬 개발에는 원래 이 방식이 더 안전하다.

1. [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) 설치 후 터미널 재시작
2. ```bash
   gcloud auth login
   gcloud config set project <프로젝트 ID>
   gcloud auth application-default login
   gcloud auth application-default set-quota-project <프로젝트 ID>
   ```
3. `.env` 의 인증 항목을 **비워 둔다**:
   ```bash
   GOOGLE_APPLICATION_CREDENTIALS=
   GCP_SA_JSON=
   ```

`src/bq.py` 는 `GCP_SA_JSON` → 키 파일 → gcloud(ADC) 순으로 자격증명을 찾으므로,
비워 두기만 하면 방식 B가 자동 적용된다.

> **GitHub Actions 자동화(Step 6)는?** 키를 못 만드는 조직이라면 Workload Identity
> Federation 으로 연결한다. 키 파일 없이 Actions 에 권한을 주는 방식이며 Step 6 에서 다룬다.

> `credentials/` 와 `.env` 는 `.gitignore`에 등록되어 있다. 절대 커밋하지 말 것.

## B. YouTube (OAuth refresh token)

BigQuery와 달리 서비스 계정으로는 안 된다. **내 채널의 Analytics를 읽는 것이므로 채널 소유자 OAuth가 필요**하다.

1. **API 및 서비스 → OAuth 동의 화면** (= Google 인증 플랫폼)
   - 사용자 유형: **내부** — 채널 계정과 같은 Workspace 조직에서 만든
     프로젝트라면 이쪽이 훨씬 간단하다. 브랜딩·게시 절차가 없고 경고 화면도 없다.
   - 사용자 유형: **외부** — 그 외의 경우. 게시하려면 브랜딩 페이지에
     홈페이지·개인정보처리방침 URL 과 Search Console 로 소유 확인된
     승인된 도메인이 필요하다. (`docs/index.html`, `docs/privacy.html` 을
     GitHub Pages 등에 올려 쓰면 된다)
   - ⚠️ 외부라면 **반드시 "게시(In production)" 상태로 전환**.
     테스트 상태면 refresh token 이 **7일 만에 만료**되어 매주 깨진다.
   - 범위에 `yt-analytics.readonly`, `youtube.readonly` 추가
   - 앱 로고는 올리지 않는다. 올리면 브랜드 검증 대상이 되어 심사가 필요해진다.
   - "확인되지 않은 앱" 경고와 100명 사용자 상한은 남지만, 동의할 계정이
     한두 개뿐이라면 심사를 받을 필요가 없다.
2. **사용자 인증 정보 → OAuth 클라이언트 ID → 데스크톱 앱**
   → `YT_CLIENT_ID`, `YT_CLIENT_SECRET` 을 `.env`에 입력
3. refresh token 발급 — 상황에 맞는 모드를 고른다.

   **채널 계정으로 이 PC에서 로그인할 수 있으면:**
   ```bash
   python scripts/get_youtube_token.py
   ```
   브라우저가 열리고, 동의하면 토큰이 출력된다.

   **채널 소유자(담당자)가 자기 PC에서 동의해야 하면:**
   ```bash
   python scripts/get_youtube_token.py --manual
   ```
   인증 URL 이 출력된다. 담당자에게 전달하면 담당자가 본인 PC에서 로그인·동의하고,
   그 뒤 이동하는 `http://localhost:8080/?code=...` 주소를 회신해 준다. 그 주소를
   스크립트에 붙여넣으면 토큰 교환이 끝난다. 담당자는 파이썬을 설치할 필요도,
   이쪽 PC를 만질 필요도 없다. (인증 코드는 약 10분 후 만료)

   출력된 `YT_REFRESH_TOKEN` 을 `.env`에 붙여넣는다.

> **YouTube Studio 의 권한 위임(관리자/편집자/뷰어)으로는 API 를 쓸 수 없다.**
> Studio 화면에서는 데이터가 보이지만 API 호출은 403 으로 막힌다. API 접근은
> 소유자(Owner) 계정의 OAuth 동의가 필요하므로, 위의 `--manual` 모드로
> 소유자에게 1회 동의를 받는 것이 가장 부담이 적다.

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

# 4) 수집 실행
python scripts/collect.py youtube              # 최근 3일 재수집 (기본)
python scripts/collect.py youtube --days 90    # 90일 소급 수집 (최초 1회)
python scripts/collect.py all                  # 구현된 모든 플랫폼
```

### 수집 동작 메모

- **YouTube Shorts 판별은 길이 + 종횡비**를 함께 본다. Data API 에 Shorts 여부
  플래그가 없어서다.
  - 길이: `YT_SHORTS_MAX_SEC`(기본 180초) 이하
  - 비율: `videos.list` 에 `part=player` 와 `maxHeight` 를 주면
    `player.embedWidth/embedHeight` 가 영상의 실제 비율로 온다.
    9:16 이면 `0.5625`, 16:9 면 `1.78`. `YT_SHORTS_MAX_ASPECT`(기본 1.0) 이하만 통과.
  - 길이만 보면 30초짜리 가로 영상이 Shorts 로 잘못 잡힌다. 비율을 함께 보면 걸러진다.
  - 계산된 비율은 `dim_content.aspect_ratio` 에 저장되므로 분류 결과를 SQL 로 감사할 수 있다.
  - 비율 기준이 후보를 **전부** 배제하면 신호 오작동으로 보고 길이 기준으로
    되돌린 뒤 `ops_run_log` 에 경고를 남긴다. 조용히 0건이 되는 사고를 막기 위함이다.
- **일별 지표는 영상 하나씩 조회**한다. Analytics API 의 `video` 차원 리포트는
  기간 합산 '상위 영상' 형태라 `day` 와 함께 쓸 수 없다. 영상이 수백 개를 넘으면
  YouTube Reporting API(벌크 CSV)로 옮기는 편이 낫다.
- **쿼터**: 영상 목록은 `search.list`(100 units) 대신 `playlistItems.list`(1 unit)와
  `videos.list`(1 unit/50개)를 쓴다. Analytics API 쿼터는 Data API 와 별도다.
- **최근 3일은 매번 다시 가져와 덮어쓴다.** YouTube 수치가 2~3일간 확정되지 않기 때문.
- **Instagram 은 누적값만 준다.** 미디어 인사이트에 날짜별 데이터가 없어서
  주기적으로 누적값을 `fact_snapshot` 에 찍고, 일별 증분은 Step 4 의 SQL 뷰에서
  차분으로 만든다. 그래서 IG 는 **자주 돌릴수록 시계열이 촘촘해진다**.
- **Meta 지표 이름은 실행 시점에 확정한다.** `config/settings.py` 의
  `IG_MEDIA_METRICS` 는 '요청해 볼 후보'일 뿐이고, `src/meta.py` 의
  `resolve_metrics` 가 실행당 한 번 호출해 보고 거부된 지표만 빼낸다.
  Meta 가 지표를 폐기해도 수집이 멈추지 않고, 무엇이 빠졌는지 `ops_run_log` 에 남는다.
  원본 응답은 `raw_json` 에 보관하므로 나중에 재해석할 수 있다.
- **`--days` 의 뜻이 플랫폼마다 다르다.** YouTube 는 *지표 날짜 범위*,
  Instagram/Facebook 은 *대상 콘텐츠의 게시일 범위*다.
- 실행 이력은 `ops_run_log` 에 남는다. 결측 구간을 찾을 때 이 테이블을 본다.

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
