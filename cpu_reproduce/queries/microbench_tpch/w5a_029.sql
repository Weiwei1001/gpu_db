-- -- q5: orders JOIN customer (~1.5M x 150K)
-- Workload: w5a | Estimated GPU time: ~9.0ms

SELECT c_mktsegment, COUNT(*), SUM(o_totalprice) FROM orders JOIN customer ON o_custkey = c_custkey GROUP BY c_mktsegment;
