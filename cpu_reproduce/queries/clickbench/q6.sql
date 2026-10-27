-- ClickBench q6 (rewrite: MIN/MAX USMALLINT → use EventTime BIGINT instead)
SELECT CAST(MIN(EventTime)/86400 AS INTEGER) AS min_date, CAST(MAX(EventTime)/86400 AS INTEGER) AS max_date FROM t;
