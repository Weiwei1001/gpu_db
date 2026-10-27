-- -- q5: arithmetic expression
-- Workload: w1 | Estimated GPU time: ~3.0ms

SELECT SUM(l_extendedprice * l_discount) FROM lineitem;
