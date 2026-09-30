SELECT SearchPhrase, MIN(URL), COUNT(*) AS c FROM t WHERE URL LIKE '%google%' AND SearchPhrase <> '' GROUP BY SearchPhrase ORDER BY c DESC LIMIT 10;
