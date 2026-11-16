SELECT SUM(l_extendedprice) FROM lineitem JOIN part ON l_partkey = p_partkey WHERE p_size < 10;
