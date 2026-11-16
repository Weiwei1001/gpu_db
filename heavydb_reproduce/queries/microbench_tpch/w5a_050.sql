SELECT COUNT(*) FROM lineitem JOIN orders ON l_orderkey = o_orderkey JOIN customer ON o_custkey = c_custkey;
