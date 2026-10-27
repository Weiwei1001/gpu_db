-- -- q4: ~25% selectivity (flag equality)
-- Workload: w2 | Estimated GPU time: ~3.6ms

SELECT COUNT(*) FROM lineitem WHERE l_returnflag = 'R';
