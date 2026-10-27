-- -- q17: sort with filter
-- Workload: w6 | Estimated GPU time: ~5.2ms

SELECT EventTime, CounterID FROM hits WHERE RegionID = 229 ORDER BY EventTime LIMIT 100;
