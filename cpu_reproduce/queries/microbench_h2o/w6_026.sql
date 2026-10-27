-- -- q11: top-1000 by v2
-- Workload: w6 | Estimated GPU time: ~20.0ms

SELECT id1, v2 FROM groupby ORDER BY v2 DESC LIMIT 1000;
