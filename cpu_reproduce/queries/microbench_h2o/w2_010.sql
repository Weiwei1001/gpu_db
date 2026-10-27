-- -- q10: ~1% selectivity (equality on low-card key)
-- Workload: w2 | Estimated GPU time: ~4.8ms

SELECT AVG(v3) FROM groupby WHERE id1 = 'id001';
