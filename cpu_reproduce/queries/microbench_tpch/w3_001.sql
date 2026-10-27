-- -- q7: 5 groups (c_mktsegment)
-- Workload: w3 | Estimated GPU time: ~1.0ms

SELECT c_mktsegment, COUNT(*), AVG(c_acctbal) FROM customer GROUP BY c_mktsegment;
