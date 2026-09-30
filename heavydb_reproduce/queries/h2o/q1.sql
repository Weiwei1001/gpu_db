SELECT id1, sum(v1) AS v1_sum FROM groupby GROUP BY id1 ORDER BY v1_sum;
