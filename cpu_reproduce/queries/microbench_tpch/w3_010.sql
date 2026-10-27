-- -- q6: 5 groups (o_orderpriority)
-- Workload: w3 | Estimated GPU time: ~3.0ms

SELECT o_orderpriority, COUNT(*) FROM orders GROUP BY o_orderpriority;
