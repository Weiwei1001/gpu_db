-- -- q3: single column average
-- Workload: w1 | Estimated GPU time: ~3.0ms

SELECT AVG(l_extendedprice) FROM lineitem;
