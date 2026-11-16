SELECT c_mktsegment, COUNT(*), AVG(c_acctbal) FROM customer GROUP BY c_mktsegment;
