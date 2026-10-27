-- case_bench q4: filter + small group-by on 25-row table.
-- Data volume too small to amortize any GPU acceleration.
SELECT n_regionkey, COUNT(*) AS count_nations
FROM nation
WHERE n_regionkey < 3
GROUP BY n_regionkey;
