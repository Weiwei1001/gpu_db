SELECT c_mktsegment, SUM(l_extendedprice) FROM lineitem JOIN orders ON l_orderkey = o_orderkey JOIN customer ON o_custkey = c_custkey GROUP BY c_mktsegment;
