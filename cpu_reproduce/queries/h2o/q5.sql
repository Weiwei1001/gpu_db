-- H2O q5: id6, sum(v1), sum(v2), sum(v3) group by id6
SELECT id6, sum(v1) AS v1, sum(v2) AS v2, sum(v3) AS v3 FROM groupby GROUP BY id6;
