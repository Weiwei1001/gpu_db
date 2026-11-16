SELECT o_orderstatus, SUM(l_quantity) FROM lineitem JOIN orders ON l_orderkey = o_orderkey GROUP BY o_orderstatus;
