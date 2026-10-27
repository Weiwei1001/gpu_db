-- -- q16: few groups (AdvEngineID, most are 0)
-- Workload: w3 | Estimated GPU time: ~7.0ms

SELECT AdvEngineID, COUNT(*) FROM t GROUP BY AdvEngineID;
