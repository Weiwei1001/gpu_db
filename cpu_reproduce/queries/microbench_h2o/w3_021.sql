-- -- q14: single key, single agg
-- Workload: w3 | Estimated GPU time: ~8.0ms

SELECT id1, COUNT(*) FROM groupby GROUP BY id1;
