SELECT l_suppkey, COUNT(*), SUM(l_extendedprice) FROM lineitem GROUP BY l_suppkey;
