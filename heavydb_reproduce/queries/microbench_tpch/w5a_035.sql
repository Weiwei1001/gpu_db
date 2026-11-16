SELECT SUM(l_extendedprice) FROM lineitem JOIN orders ON l_orderkey = o_orderkey;
