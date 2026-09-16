-- ============================================================================
-- shortform-kpi BigQuery 스키마
--   ${DATASET} 는 src/bq.py 가 "project.dataset" 으로 치환한다.
--
-- 설계 원칙
--   1) 모든 테이블에 대리 키(*_key)를 두고 MERGE(UPSERT)로만 적재한다.
--      → 같은 수집을 두 번 돌려도 중복이 생기지 않는다(멱등성).
--   2) raw_json 에 API 원본 응답을 그대로 보관한다.
--      → Meta 가 지표 이름을 바꿔도 과거 데이터를 다시 해석할 수 있다.
--   3) 날짜 파티셔닝 + platform 클러스터링으로 스캔 비용을 억제한다.
-- ============================================================================

-- 계정 마스터 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `${DATASET}.dim_account` (
  account_key   STRING NOT NULL OPTIONS(description="platform:account_id"),
  platform      STRING NOT NULL OPTIONS(description="youtube | instagram | facebook"),
  account_id    STRING NOT NULL,
  name          STRING,
  handle        STRING,
  updated_at    TIMESTAMP NOT NULL
)
CLUSTER BY platform
OPTIONS(description="수집 대상 채널/계정 마스터");


-- 콘텐츠 마스터 (불변 속성) ------------------------------------------------
CREATE TABLE IF NOT EXISTS `${DATASET}.dim_content` (
  content_key    STRING NOT NULL OPTIONS(description="platform:content_id"),
  platform       STRING NOT NULL,
  content_id     STRING NOT NULL,
  account_id     STRING,
  published_at   TIMESTAMP,
  published_date DATE,
  duration_sec   INT64,
  aspect_ratio   FLOAT64 OPTIONS(description="width/height. 9:16=0.5625, 16:9=1.78"),
  title          STRING,
  caption        STRING,
  hashtags       ARRAY<STRING>,
  permalink      STRING,
  thumbnail_url  STRING,
  media_format   STRING OPTIONS(description="SHORTS | REELS"),
  first_seen_at  TIMESTAMP,
  updated_at     TIMESTAMP,
  raw_json       STRING
)
PARTITION BY published_date
CLUSTER BY platform, account_id
OPTIONS(description="숏폼 콘텐츠 마스터. 게시 후 변하지 않는 속성만 보관");


-- 스냅샷: 수집 시점의 '누적' 지표 ------------------------------------------
-- Instagram/Facebook 은 누적값만 주므로 이 테이블의 차분으로 일별을 만든다.
CREATE TABLE IF NOT EXISTS `${DATASET}.fact_snapshot` (
  snapshot_key       STRING NOT NULL OPTIONS(description="content_key + 수집시각(시간 단위) 해시"),
  content_key        STRING NOT NULL,
  platform           STRING NOT NULL,
  content_id         STRING NOT NULL,
  collected_at       TIMESTAMP NOT NULL,
  collected_date     DATE NOT NULL,
  views              INT64,
  reach              INT64,
  likes              INT64,
  comments           INT64,
  shares             INT64,
  saves              INT64,
  total_interactions INT64,
  watch_time_sec     FLOAT64,
  avg_watch_sec      FLOAT64,
  raw_json           STRING,
  loaded_at          TIMESTAMP NOT NULL
)
PARTITION BY collected_date
CLUSTER BY platform, content_key
OPTIONS(description="수집 시점 누적 지표. IG/FB 일별 지표의 원천");


-- 일별 지표 ----------------------------------------------------------------
-- YouTube 는 Analytics API 가 일별을 직접 주므로 source='api',
-- IG/FB 는 스냅샷 차분으로 채우므로 source='diff'.
CREATE TABLE IF NOT EXISTS `${DATASET}.fact_daily` (
  daily_key          STRING NOT NULL OPTIONS(description="content_key + metric_date"),
  content_key        STRING NOT NULL,
  platform           STRING NOT NULL,
  content_id         STRING NOT NULL,
  metric_date        DATE NOT NULL,
  views              INT64,
  reach              INT64,
  likes              INT64,
  comments           INT64,
  shares             INT64,
  saves              INT64,
  total_interactions INT64,
  watch_time_sec     FLOAT64,
  avg_watch_sec      FLOAT64,
  avg_view_pct       FLOAT64,
  followers_gained   INT64,
  source             STRING NOT NULL OPTIONS(description="api | diff"),
  loaded_at          TIMESTAMP NOT NULL
)
PARTITION BY metric_date
CLUSTER BY platform, content_key
OPTIONS(description="콘텐츠×일자 단위 증분 지표");


-- 계정 스냅샷 (팔로워 수) ---------------------------------------------------
-- 도달률 = reach / followers 계산에 필요하다.
CREATE TABLE IF NOT EXISTS `${DATASET}.fact_account_snapshot` (
  account_snapshot_key STRING NOT NULL,
  account_key          STRING NOT NULL,
  platform             STRING NOT NULL,
  account_id           STRING NOT NULL,
  collected_at         TIMESTAMP NOT NULL,
  collected_date       DATE NOT NULL,
  followers            INT64,
  media_count          INT64,
  raw_json             STRING,
  loaded_at            TIMESTAMP NOT NULL
)
PARTITION BY collected_date
CLUSTER BY platform
OPTIONS(description="계정 단위 팔로워/게시물 수 스냅샷");


-- 수집 실행 로그 ------------------------------------------------------------
-- 조용히 실패해서 며칠치가 비는 사고를 잡기 위한 테이블.
CREATE TABLE IF NOT EXISTS `${DATASET}.ops_run_log` (
  run_id       STRING NOT NULL,
  platform     STRING NOT NULL,
  started_at   TIMESTAMP NOT NULL,
  finished_at  TIMESTAMP,
  run_date     DATE NOT NULL,
  status       STRING OPTIONS(description="success | failed"),
  rows_written INT64,
  message      STRING
)
PARTITION BY run_date
CLUSTER BY platform
OPTIONS(description="수집 실행 이력. 결측 구간 탐지용");
