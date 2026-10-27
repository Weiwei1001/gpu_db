-- -- q2: ~10K groups (l_suppkey)
-- Workload: w4 | Estimated GPU time: ~24.0ms

SELECT l_suppkey, COUNT(*), SUM(l_extendedprice) FROM lineitem GROUP BY l_suppkey;
