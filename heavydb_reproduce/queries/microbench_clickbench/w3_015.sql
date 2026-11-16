SELECT TraficSourceID, COUNT(*), SUM(GoodEvent) FROM t GROUP BY TraficSourceID;
