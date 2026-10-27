-- -- q13: sort with filter
-- Workload: w6 | Estimated GPU time: ~6.0ms

SELECT id1, v3 FROM groupby WHERE id4 > 50 ORDER BY v3 DESC LIMIT 100;
