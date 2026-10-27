-- -- q20: compound predicate
-- Workload: w2 | Estimated GPU time: ~4.2ms

SELECT SUM(GoodEvent) FROM hits WHERE CounterID > 10000 AND RegionID > 100;
