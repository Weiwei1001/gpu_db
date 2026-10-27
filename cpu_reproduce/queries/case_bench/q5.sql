-- case_bench q5: narrow top-N on orders. Sort + LIMIT pulls tiny output;
-- CPU can use a partial heap-sort and skips most of the key comparisons.
SELECT o_orderkey, o_orderdate
FROM orders
ORDER BY o_orderdate ASC
LIMIT 10;
