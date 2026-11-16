SELECT l_returnflag, l_linestatus, COUNT(*), SUM(l_quantity), AVG(l_extendedprice) FROM lineitem GROUP BY l_returnflag, l_linestatus;
