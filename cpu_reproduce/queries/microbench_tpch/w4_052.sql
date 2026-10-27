-- -- q1: ~200K groups (l_partkey, 200K distinct values at SF=1)
-- Workload: w4 | Estimated GPU time: ~24.0ms

SELECT l_partkey, SUM(l_quantity) FROM lineitem GROUP BY l_partkey;
