SELECT l_linestatus, SUM(l_extendedprice), AVG(l_discount) FROM lineitem GROUP BY l_linestatus;
