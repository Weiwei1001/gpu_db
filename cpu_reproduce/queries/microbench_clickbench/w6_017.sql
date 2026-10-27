-- -- q16: top-100 by WatchID
-- Workload: w6 | Estimated GPU time: ~10.5ms

SELECT WatchID FROM t ORDER BY WatchID DESC LIMIT 100;
