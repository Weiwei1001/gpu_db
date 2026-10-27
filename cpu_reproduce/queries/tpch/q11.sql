-- 原始 Q11，聚合与子查询结果 CAST 为 DOUBLE 以在 Sirius GPU 上兼容（避免 DECIMAL）
SELECT
    ps_partkey,
    CAST(sum(ps_supplycost * ps_availqty) AS DOUBLE) AS value
FROM
    partsupp,
    supplier,
    nation
WHERE
    ps_suppkey = s_suppkey
    AND s_nationkey = n_nationkey
    AND n_name = 'GERMANY'
GROUP BY
    ps_partkey
HAVING
    CAST(sum(ps_supplycost * ps_availqty) AS DOUBLE) > (
        SELECT
            CAST(sum(ps_supplycost * ps_availqty) * 0.0001000000 AS DOUBLE)
        FROM
            partsupp,
            supplier,
            nation
        WHERE
            ps_suppkey = s_suppkey
            AND s_nationkey = n_nationkey
            AND n_name = 'GERMANY')
ORDER BY
    value DESC;
