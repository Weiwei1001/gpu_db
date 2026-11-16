SELECT s_nationkey, SUM(l_extendedprice) FROM lineitem JOIN supplier ON l_suppkey = s_suppkey GROUP BY s_nationkey;
