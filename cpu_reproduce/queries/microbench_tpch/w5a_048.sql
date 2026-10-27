-- -- q4: lineitem JOIN orders + group by
-- Workload: w5a | Estimated GPU time: ~18.0ms

SELECT o_orderstatus, SUM(l_quantity) FROM lineitem JOIN orders ON l_orderkey = o_orderkey GROUP BY o_orderstatus;
