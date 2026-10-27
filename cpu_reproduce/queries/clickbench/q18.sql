-- ClickBench q18 (rewrite: to_timestamp → integer arithmetic for GPU)
SELECT UserID, CAST((EventTime % 3600) / 60 AS INTEGER) AS m, SearchPhrase, COUNT(*) FROM t GROUP BY UserID, m, SearchPhrase ORDER BY COUNT(*) DESC LIMIT 10;
