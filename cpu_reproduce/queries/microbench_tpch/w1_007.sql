-- -- q4: min + max
-- Workload: w1 | Estimated GPU time: ~3.0ms

SELECT MIN(l_discount), MAX(l_discount) FROM lineitem;
