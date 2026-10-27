-- -- q14: compound key id4 x id6
-- Workload: w4 | Estimated GPU time: ~64.0ms

SELECT id4, id6, SUM(v1) FROM groupby GROUP BY id4, id6;
