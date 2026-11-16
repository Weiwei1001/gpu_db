SELECT l_orderkey, l_extendedprice * (1 - l_discount) AS net_price FROM lineitem ORDER BY net_price DESC LIMIT 1000;
