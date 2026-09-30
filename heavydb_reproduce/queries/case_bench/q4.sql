SELECT n_regionkey, COUNT(*) AS count_nations FROM nation WHERE n_regionkey < 3 GROUP BY n_regionkey;
