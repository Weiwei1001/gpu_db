SELECT SearchPhrase, COUNT(*) AS c FROM t WHERE SearchPhrase <> '' GROUP BY SearchPhrase ORDER BY c DESC LIMIT 10;
