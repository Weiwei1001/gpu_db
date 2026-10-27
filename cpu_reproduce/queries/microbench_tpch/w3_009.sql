-- -- q5: 3 groups (o_orderstatus)
-- Workload: w3 | Estimated GPU time: ~3.0ms

SELECT o_orderstatus, COUNT(*), SUM(o_totalprice) FROM orders GROUP BY o_orderstatus;
