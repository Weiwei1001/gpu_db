SELECT CounterID, EventDate, COUNT(*) AS cnt FROM t GROUP BY CounterID, EventDate ORDER BY cnt DESC LIMIT 100;
