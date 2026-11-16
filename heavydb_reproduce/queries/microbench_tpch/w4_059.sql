SELECT l_suppkey, MIN(l_extendedprice), MAX(l_extendedprice), AVG(l_quantity) FROM lineitem GROUP BY l_suppkey;
