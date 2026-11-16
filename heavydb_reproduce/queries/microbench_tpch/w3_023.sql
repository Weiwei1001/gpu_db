SELECT l_returnflag, COUNT(*), SUM(l_quantity) FROM lineitem GROUP BY l_returnflag;
