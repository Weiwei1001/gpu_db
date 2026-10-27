-- ClickBench q14
SELECT SearchEngineID, SearchPhrase, COUNT(*) AS c FROM t WHERE SearchPhrase <> '' GROUP BY SearchEngineID, SearchPhrase ORDER BY c DESC LIMIT 10;
