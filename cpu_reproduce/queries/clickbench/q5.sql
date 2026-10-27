-- ClickBench q5 (rewrite: COUNT(DISTINCT) → GROUP BY subquery for GPU)
SELECT COUNT(*) FROM (SELECT SearchPhrase FROM t GROUP BY SearchPhrase) sub;
