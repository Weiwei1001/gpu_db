SELECT l_shipmode, COUNT(*), SUM(l_extendedprice) FROM lineitem GROUP BY l_shipmode;
