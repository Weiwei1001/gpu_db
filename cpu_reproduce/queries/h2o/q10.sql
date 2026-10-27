-- H2O q10: all ids, sum(v3), count group by all ids
SELECT id1, id2, id3, id4, id5, id6, sum(v3) AS v3, count(*) AS cnt FROM groupby GROUP BY id1, id2, id3, id4, id5, id6;
