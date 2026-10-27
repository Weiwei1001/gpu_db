-- -- q20: ~few groups (TraficSourceID)
-- Workload: w3 | Estimated GPU time: ~7.0ms

SELECT TraficSourceID, COUNT(*), SUM(GoodEvent) FROM t GROUP BY TraficSourceID;
