-- -- q2: 4 groups (l_linestatus)
-- Workload: w3 | Estimated GPU time: ~6.0ms

SELECT l_linestatus, SUM(l_extendedprice), AVG(l_discount) FROM lineitem GROUP BY l_linestatus;
