-- -- q17: few groups (OS)
-- Workload: w3 | Estimated GPU time: ~7.0ms

SELECT OS, COUNT(*) FROM t GROUP BY OS;
