-- -- q9: top-10 by v3
-- Workload: w6 | Estimated GPU time: ~8.0ms

SELECT id1, v3 FROM groupby ORDER BY v3 DESC LIMIT 10;
