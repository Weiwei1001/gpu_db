-- -- q3: ~33% selectivity (date range)
-- Workload: w2 | Estimated GPU time: ~3.6ms

SELECT AVG(l_quantity) FROM lineitem WHERE l_shipdate >= '1995-01-01';
