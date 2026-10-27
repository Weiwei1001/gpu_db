-- -- q16: medium selectivity (region filter)
-- Workload: w2 | Estimated GPU time: ~4.2ms

SELECT SUM(ResolutionWidth) FROM hits WHERE RegionID = 229;
