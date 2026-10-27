-- -- q4: ~150K groups (c_custkey)
-- Workload: w4 | Estimated GPU time: ~12.0ms

SELECT o_custkey, COUNT(*), SUM(o_totalprice) FROM orders GROUP BY o_custkey;
