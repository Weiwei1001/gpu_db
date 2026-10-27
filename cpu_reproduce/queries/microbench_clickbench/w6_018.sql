-- -- q19: top-100 with compound sort key
-- Workload: w6 | Estimated GPU time: ~10.5ms

SELECT CounterID, EventDate, COUNT(*) AS cnt FROM t GROUP BY CounterID, EventDate ORDER BY cnt DESC LIMIT 100;
