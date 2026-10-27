-- -- q1: full sort single column (numeric)
-- Workload: w6 | Estimated GPU time: ~60.0ms

SELECT l_extendedprice FROM lineitem ORDER BY l_extendedprice;
