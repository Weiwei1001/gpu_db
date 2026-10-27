-- -- q10: top-100 by v1
-- Workload: w6 | Estimated GPU time: ~12.0ms

SELECT id1, id2, v1 FROM groupby ORDER BY v1 DESC LIMIT 100;
