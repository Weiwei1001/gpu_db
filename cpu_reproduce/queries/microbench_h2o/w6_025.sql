-- -- q12: sort by compound expression
-- Workload: w6 | Estimated GPU time: ~12.0ms

SELECT id1, v1 + v2 AS total FROM groupby ORDER BY total DESC LIMIT 100;
