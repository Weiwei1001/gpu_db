-- -- q1: 2 groups (l_returnflag)
-- Workload: w3 | Estimated GPU time: ~6.0ms

SELECT l_returnflag, COUNT(*), SUM(l_quantity) FROM lineitem GROUP BY l_returnflag;
