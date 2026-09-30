SELECT COUNT(*) FROM (SELECT SearchPhrase FROM t GROUP BY SearchPhrase) sub;
