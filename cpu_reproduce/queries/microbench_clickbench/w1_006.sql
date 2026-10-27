-- -- q20: multi-column aggregation
-- Workload: w1 | Estimated GPU time: ~4.2ms

SELECT SUM(ResolutionWidth), AVG(ResolutionHeight), MIN(ClientIP), MAX(UserID) FROM t;
