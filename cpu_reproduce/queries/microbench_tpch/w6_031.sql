-- -- q3: top-100 by quantity
-- Workload: w6 | Estimated GPU time: ~9.0ms

SELECT l_orderkey, l_quantity FROM lineitem ORDER BY l_quantity DESC LIMIT 100;
