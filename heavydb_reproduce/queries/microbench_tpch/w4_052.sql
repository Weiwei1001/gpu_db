SELECT l_partkey, SUM(l_quantity) FROM lineitem GROUP BY l_partkey;
