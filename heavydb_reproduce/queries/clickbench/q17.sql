SELECT UserID, SearchPhrase, COUNT(*) FROM t GROUP BY UserID, SearchPhrase LIMIT 10;
