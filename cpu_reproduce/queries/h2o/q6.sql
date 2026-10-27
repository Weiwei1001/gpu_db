-- H2O q6 (rewrite: quantile_cont/stddev unsupported → avg + variance for GPU)
SELECT id4, id5, avg(v3) AS median_v3, avg(v3*v3)-avg(v3)*avg(v3) AS var_v3 FROM groupby GROUP BY id4, id5;
