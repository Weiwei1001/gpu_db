SELECT l_orderkey, SUM(l_quantity), SUM(l_extendedprice) FROM lineitem GROUP BY l_orderkey;
