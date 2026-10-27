-- case_bench q1: tiny full scan (region has 5 rows regardless of SF).
-- GPU pays kernel launch + transfer overhead for zero compute gain.
SELECT r_regionkey, r_name, r_comment FROM region;
