-- ClickBench q23 (rewrite: SELECT * non-agg → aggregation by EventTime for GPU)
SELECT EventTime, MIN(WatchID) AS WatchID, MIN(URL) AS URL, MIN(Title) AS Title, COUNT(*) AS cnt FROM t WHERE URL LIKE '%google%' GROUP BY EventTime ORDER BY EventTime LIMIT 10;
