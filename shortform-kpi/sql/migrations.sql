-- ============================================================================
-- 이미 생성된 테이블에 적용할 변경사항.
-- schema.sql 의 CREATE TABLE IF NOT EXISTS 는 기존 테이블을 건드리지 않으므로,
-- 컬럼 추가는 여기에 ALTER TABLE 로 적는다. 모두 IF NOT EXISTS 라 몇 번
-- 실행해도 안전하다. init_bigquery.py 가 schema.sql 다음에 이 파일을 돌린다.
-- ============================================================================

ALTER TABLE `${DATASET}.dim_content`
  ADD COLUMN IF NOT EXISTS aspect_ratio FLOAT64
  OPTIONS(description="width/height. 9:16=0.5625, 16:9=1.78");
