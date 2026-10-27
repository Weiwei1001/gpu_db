-- H2O q1: id1, sum(v1) group by id1
SELECT id1, sum(v1) AS v1_sum FROM groupby GROUP BY id1 ORDER BY v1_sum;
