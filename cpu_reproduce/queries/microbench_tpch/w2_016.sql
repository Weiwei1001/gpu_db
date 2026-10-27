-- -- q5: ~2% selectivity (compound predicate)
-- Workload: w2 | Estimated GPU time: ~3.6ms

SELECT SUM(l_extendedprice * l_discount) FROM lineitem WHERE l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24;
