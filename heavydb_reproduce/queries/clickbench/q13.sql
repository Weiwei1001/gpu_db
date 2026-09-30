SELECT SearchPhrase, COUNT(DISTINCT UserID) AS u FROM t WHERE SearchPhrase <> '' GROUP BY SearchPhrase ORDER BY u DESC LIMIT 10;
