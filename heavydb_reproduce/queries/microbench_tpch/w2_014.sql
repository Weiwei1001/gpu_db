SELECT AVG(l_quantity) FROM lineitem WHERE l_shipdate >= CAST('1995-01-01' AS date);
