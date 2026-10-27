-- -- q18: boolean filter
-- Workload: w2 | Estimated GPU time: ~4.2ms

SELECT AVG(ResolutionWidth) FROM t WHERE GoodEvent = 1;
