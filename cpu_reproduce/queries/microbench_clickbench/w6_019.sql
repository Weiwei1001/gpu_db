-- -- q20: sort by ResolutionWidth
-- Workload: w6 | Estimated GPU time: ~10.5ms

SELECT WatchID, ResolutionWidth FROM t ORDER BY ResolutionWidth DESC LIMIT 100;
