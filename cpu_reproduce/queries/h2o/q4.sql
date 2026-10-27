-- H2O q4: id4, avg(v1), avg(v2), avg(v3) group by id4
SELECT id4, avg(v1) AS v1, avg(v2) AS v2, avg(v3) AS v3 FROM groupby GROUP BY id4;
