--Parameters table for the decision day
CREATE OR REPLACE TABLE params AS
SELECT DATE '2025-11-12' AS as_of;

--Features table
CREATE OR REPLACE TABLE features AS
WITH p AS (
  SELECT as_of FROM params
),
hist AS (                                   -- only history before the decision day
  SELECT t.* FROM attempts t, p WHERE t.attempt_day < p.as_of
),
per_loan AS (
  SELECT
    loan_id,
    count(*)                                                        AS attempts_total,
    count(*) FILTER (WHERE attempt_day >= (SELECT as_of FROM p) - INTERVAL 30 DAY) AS attempts_30d,
    max(attempt_day)                                                AS last_attempt_day,
    arg_max(outcome, created_utc)                                   AS last_outcome,
    arg_max(result_utc, created_utc)                                AS last_result_utc,
    count(*) FILTER (WHERE n_success > 0)                           AS success_count,
    max(attempt_day) FILTER (WHERE n_success > 0)                   AS last_success_day,
    arg_max(success_amount, created_utc) FILTER (WHERE n_success > 0) AS last_success_amount,
    max(result_utc) FILTER (WHERE n_success > 0)                    AS last_success_result_utc,
    sum(n_chargeback)                                               AS chargeback_count,
    count(*) FILTER (WHERE outcome = 'BLOCKED')                     AS blocked_count,
    max(attempt_day) FILTER (WHERE outcome = 'BLOCKED')             AS last_blocked_day,
    count(*) FILTER (WHERE outcome = 'WAIT')                        AS wait_count,
    max(attempt_day) FILTER (WHERE outcome = 'WAIT')                AS last_wait_day,
    count(*) FILTER (WHERE outcome = 'HARD')                        AS hard_count,
    count(*) FILTER (WHERE outcome = 'REVOKED')                     AS revoked_count,
    bool_or(result_utc IS NULL
            OR result_utc >= CAST((SELECT as_of FROM p) AS TIMESTAMP)) AS has_pending,
    list(day(attempt_day) ORDER BY attempt_day) FILTER (WHERE n_success > 0) AS success_days_of_month
  FROM hist
  GROUP BY loan_id
),
cb AS (
  SELECT loan_id, max(chargeback_utc) AS last_chargeback_utc
  FROM requests
  WHERE status = 'chargeback'
  GROUP BY loan_id
)
SELECT
  l.loan_id,
  l.bank,
  l.loan_amount,
  l.total_amount_outstanding,
  l.overdue_at,
  date_diff('day', l.overdue_at, p.as_of)                           AS days_overdue_as_of,
  CASE WHEN date_diff('day', l.overdue_at, p.as_of) <= 90  THEN '1-90'
       WHEN date_diff('day', l.overdue_at, p.as_of) <= 180 THEN '91-180'
       WHEN date_diff('day', l.overdue_at, p.as_of) <= 365 THEN '181-365'
       ELSE '365+' END                                              AS overdue_segment,
  coalesce(pl.attempts_total, 0)                                    AS attempts_total,
  coalesce(pl.attempts_30d, 0)                                      AS attempts_30d,
  pl.last_attempt_day,
  date_diff('day', pl.last_attempt_day, p.as_of)                    AS days_since_last_attempt,
  pl.last_outcome,
  pl.last_result_utc,
  coalesce(pl.success_count, 0) > 0                                 AS ever_paid,
  coalesce(pl.success_count, 0)                                     AS success_count,
  pl.last_success_day,
  pl.last_success_amount,
    pl.last_success_result_utc,
  coalesce(pl.chargeback_count, 0)                                  AS chargeback_count,
  cb.last_chargeback_utc,
  coalesce(pl.blocked_count, 0)                                     AS blocked_count,
  pl.last_blocked_day,
  coalesce(pl.wait_count, 0)                                        AS wait_count,
  pl.last_wait_day,
  coalesce(pl.hard_count, 0)                                        AS hard_count,
  coalesce(pl.revoked_count, 0)                                     AS revoked_count,
  coalesce(pl.has_pending, false)                                   AS has_pending,
  pl.success_days_of_month,
  p.as_of
FROM loans l
CROSS JOIN p
LEFT JOIN per_loan pl USING (loan_id)
LEFT JOIN cb USING (loan_id);