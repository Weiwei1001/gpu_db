-- -- q20: date + counter (medium-high cardinality)
-- Workload: w4 | Estimated GPU time: ~56.0ms

SELECT EventDate, CounterID, COUNT(*) FROM t GROUP BY EventDate, CounterID;
