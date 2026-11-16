SELECT RefererHash, COUNT(*), SUM(GoodEvent) FROM t GROUP BY RefererHash;
