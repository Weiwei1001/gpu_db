-- -- q15: ~3 groups (GoodEvent)
-- Workload: w3 | Estimated GPU time: ~7.0ms

SELECT GoodEvent, COUNT(*) FROM t GROUP BY GoodEvent;
