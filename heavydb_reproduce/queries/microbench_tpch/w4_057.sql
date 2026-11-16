SELECT l_partkey, l_shipmode, AVG(l_discount) FROM lineitem GROUP BY l_partkey, l_shipmode;
