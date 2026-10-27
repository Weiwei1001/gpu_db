-- case_bench q3: small-cardinality group-by (nation has 25 rows, 5 groups).
-- Hashing such a tiny set is dwarfed by GPU kernel fixed overhead.
SELECT n_regionkey, COUNT(*) AS count_nations FROM nation GROUP BY n_regionkey;
