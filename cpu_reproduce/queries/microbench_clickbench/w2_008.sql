-- -- q17: date range (~3% selectivity)
-- Workload: w2 | Estimated GPU time: ~4.2ms

SELECT COUNT(*) FROM t WHERE EventDate >= '2013-07-15' AND EventDate < '2013-08-01';
