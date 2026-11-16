SELECT l_shipdate, COUNT(*), SUM(l_quantity) FROM lineitem GROUP BY l_shipdate;
