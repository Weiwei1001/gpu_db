SELECT MobilePhoneModel, APPROX_COUNT_DISTINCT(UserID) AS u FROM t WHERE MobilePhoneModel <> '' GROUP BY MobilePhoneModel ORDER BY u DESC LIMIT 10;
