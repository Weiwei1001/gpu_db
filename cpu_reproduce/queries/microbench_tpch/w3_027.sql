-- -- q19: ~250 groups (RegionID top values)
-- Workload: w3 | Estimated GPU time: ~7.0ms

SELECT RegionID, COUNT(*) FROM hits GROUP BY RegionID;
