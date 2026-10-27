-- -- q18: min max timestamp
-- Workload: w1 | Estimated GPU time: ~3.5ms

SELECT MIN(EventTime), MAX(EventTime) FROM t;
