SELECT SUM(l_extendedprice * l_discount) FROM lineitem WHERE l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24;
