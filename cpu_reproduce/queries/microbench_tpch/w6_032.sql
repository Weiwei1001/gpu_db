-- -- q5: sort by date
-- Workload: w6 | Estimated GPU time: ~9.0ms

SELECT l_orderkey, l_shipdate FROM lineitem ORDER BY l_shipdate LIMIT 100;
