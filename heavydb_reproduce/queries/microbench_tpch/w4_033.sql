SELECT o_custkey, COUNT(*), SUM(o_totalprice) FROM orders GROUP BY o_custkey;
