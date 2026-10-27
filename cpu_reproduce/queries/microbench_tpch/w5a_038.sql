-- -- q10: 3-table: orders-customer-nation
-- Workload: w5a | Estimated GPU time: ~13.5ms

SELECT n_name, COUNT(*), SUM(o_totalprice) FROM orders JOIN customer ON o_custkey = c_custkey JOIN nation ON c_nationkey = n_nationkey GROUP BY n_name;
