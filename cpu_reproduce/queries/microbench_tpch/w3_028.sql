-- -- q3: ~6 groups (returnflag x linestatus)
-- Workload: w3 | Estimated GPU time: ~7.2ms

SELECT l_returnflag, l_linestatus, COUNT(*), SUM(l_quantity), AVG(l_extendedprice) FROM lineitem GROUP BY l_returnflag, l_linestatus;
