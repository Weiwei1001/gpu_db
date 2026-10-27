-- -- q18: top-1000 by UserID
-- Workload: w6 | Estimated GPU time: ~17.5ms

SELECT UserID, CounterID FROM t ORDER BY UserID DESC LIMIT 1000;
