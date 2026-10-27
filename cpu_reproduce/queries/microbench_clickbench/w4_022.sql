-- -- q16: ~many groups (UserID)
-- Workload: w4 | Estimated GPU time: ~28.0ms

SELECT UserID, COUNT(*) FROM t GROUP BY UserID;
