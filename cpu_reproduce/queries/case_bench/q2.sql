-- case_bench q2: point lookup by primary key on nation (25 rows).
-- CPU short-circuits; GPU does full scan + kernel launch for 1 row.
SELECT n_name FROM nation WHERE n_nationkey = 5;
