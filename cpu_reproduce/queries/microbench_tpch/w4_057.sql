-- -- q5: ~200K x 7 = high cardinality compound key
-- Workload: w4 | Estimated GPU time: ~48.0ms

SELECT l_partkey, l_shipmode, AVG(l_discount) FROM lineitem GROUP BY l_partkey, l_shipmode;
