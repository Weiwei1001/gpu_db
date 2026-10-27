-- H2O q2: id1, id2, sum(v1) group by id1, id2
SELECT id1, id2, sum(v1) AS v1 FROM groupby GROUP BY id1, id2;
