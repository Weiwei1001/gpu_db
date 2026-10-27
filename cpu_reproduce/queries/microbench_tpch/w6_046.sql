-- -- q4: top-1000 by compound expression
-- Workload: w6 | Estimated GPU time: ~15.0ms

SELECT l_orderkey, l_extendedprice * (1 - l_discount) AS net_price FROM lineitem ORDER BY net_price DESC LIMIT 1000;
