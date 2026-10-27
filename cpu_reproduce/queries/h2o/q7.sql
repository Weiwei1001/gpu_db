-- H2O q7: id3, max(v1)-min(v2) group by id3
SELECT id3, max(v1)-min(v2) AS range_v1_v2 FROM groupby GROUP BY id3;
