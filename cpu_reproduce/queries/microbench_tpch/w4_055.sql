-- -- q7: date group (~2500 distinct dates)
-- Workload: w4 | Estimated GPU time: ~24.0ms

SELECT l_shipdate, COUNT(*), SUM(l_quantity) FROM lineitem GROUP BY l_shipdate;
