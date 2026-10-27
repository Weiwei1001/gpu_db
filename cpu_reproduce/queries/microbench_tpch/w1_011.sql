-- -- q6: multi-column aggregation
-- Workload: w1 | Estimated GPU time: ~3.6ms

SELECT SUM(l_quantity), SUM(l_extendedprice), AVG(l_discount), AVG(l_tax) FROM lineitem;
