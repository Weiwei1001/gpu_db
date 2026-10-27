-- -- q6: orders sort by price
-- Workload: w6 | Estimated GPU time: ~4.5ms

SELECT o_orderkey, o_totalprice FROM orders ORDER BY o_totalprice DESC LIMIT 100;
