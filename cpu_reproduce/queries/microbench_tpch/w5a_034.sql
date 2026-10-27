-- -- q1: lineitem JOIN orders (largest 2-table join, ~6M x 1.5M)
-- Workload: w5a | Estimated GPU time: ~12.0ms

SELECT COUNT(*) FROM lineitem JOIN orders ON l_orderkey = o_orderkey;
