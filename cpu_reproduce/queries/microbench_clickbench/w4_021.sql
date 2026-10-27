-- -- q15: ~500K+ groups (CounterID)
-- Workload: w4 | Estimated GPU time: ~28.0ms

SELECT CounterID, COUNT(*) FROM t GROUP BY CounterID;
