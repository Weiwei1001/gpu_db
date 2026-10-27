-- -- q4: 7 groups (l_shipmode)
-- Workload: w3 | Estimated GPU time: ~6.0ms

SELECT l_shipmode, COUNT(*), SUM(l_extendedprice) FROM lineitem GROUP BY l_shipmode;
