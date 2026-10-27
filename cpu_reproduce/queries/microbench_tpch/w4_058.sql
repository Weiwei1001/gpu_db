-- -- q19: high cardinality compound key
-- Workload: w4 | Estimated GPU time: ~56.0ms

SELECT CounterID, RegionID, COUNT(*) FROM hits GROUP BY CounterID, RegionID;
