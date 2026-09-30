SELECT MobilePhone, MobilePhoneModel, COUNT(DISTINCT UserID) AS u FROM t WHERE MobilePhoneModel <> '' GROUP BY MobilePhone, MobilePhoneModel ORDER BY u DESC LIMIT 10;
