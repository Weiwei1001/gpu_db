-- H2O q3: id3, sum(v1), avg(v3) group by id3
SELECT id3, sum(v1) AS v1, avg(v3) AS v3 FROM groupby GROUP BY id3;
