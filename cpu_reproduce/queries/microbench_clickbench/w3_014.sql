-- -- q18: few groups (ResolutionDepth)
-- Workload: w3 | Estimated GPU time: ~7.0ms

SELECT ResolutionDepth, COUNT(*), AVG(ResolutionWidth) FROM t GROUP BY ResolutionDepth;
