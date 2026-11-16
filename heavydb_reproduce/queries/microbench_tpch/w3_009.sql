SELECT o_orderstatus, COUNT(*), SUM(o_totalprice) FROM orders GROUP BY o_orderstatus;
