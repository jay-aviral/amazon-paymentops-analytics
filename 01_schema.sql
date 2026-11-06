-- =====================================================================
-- Amazon PaymentOps Analytics  |  01_schema.sql  |  MySQL 8
-- Six core tables. Fictional data only.
--
--   payment_methods        reference: method, fee %, settlement days, refund SLA
--   customers              one row per customer who has placed an order
--   orders                 one row per order (the business event)
--   payments               one row per payment ATTEMPT (internal ledger view)
--   gateway_transactions   what the payment gateway says happened (external view)
--   refunds                refund requests and their processing state
--
-- Relationship:  customers 1-* orders 1-* payments 1-* gateway_transactions
--                payments 1-* refunds ; payment_methods 1-* payments
--
-- gateway_transactions.payment_id is NOT unique on purpose: a double capture
-- shows up as two gateway rows for one payment, which reconciliation must catch.
-- =====================================================================

DROP TABLE IF EXISTS refunds;
DROP TABLE IF EXISTS gateway_transactions;
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS payment_methods;

CREATE TABLE payment_methods (
    method_id        TINYINT       NOT NULL PRIMARY KEY,
    method_name      VARCHAR(20)   NOT NULL UNIQUE,
    method_type      VARCHAR(30)   NOT NULL,
    gateway_fee_pct  DECIMAL(5,2)  NOT NULL,
    settlement_days  TINYINT       NOT NULL,
    refund_sla_days  TINYINT       NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE customers (
    customer_id  BIGINT       NOT NULL PRIMARY KEY,
    signup_date  DATE         NOT NULL,
    state        VARCHAR(40)  NOT NULL,
    city_tier    VARCHAR(10)  NOT NULL,
    is_prime     TINYINT      NOT NULL,
    CONSTRAINT chk_customers_prime CHECK (is_prime IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE orders (
    order_id      BIGINT         NOT NULL PRIMARY KEY,
    customer_id   BIGINT         NOT NULL,
    order_ts      DATETIME       NOT NULL,
    order_date    DATE           NOT NULL,
    order_amount  DECIMAL(12,2)  NOT NULL,
    currency      CHAR(3)        NOT NULL,
    category      VARCHAR(30)    NOT NULL,
    order_status  VARCHAR(20)    NOT NULL,
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT chk_orders_amount  CHECK (order_amount > 0),
    CONSTRAINT chk_orders_status  CHECK (order_status IN
        ('DELIVERED','IN_TRANSIT','CANCELLED','RETURNED','PAYMENT_FAILED','PAYMENT_PENDING'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE payments (
    payment_id        BIGINT         NOT NULL PRIMARY KEY,
    order_id          BIGINT         NOT NULL,
    customer_id       BIGINT         NOT NULL,
    method_id         TINYINT        NOT NULL,
    attempt_no        TINYINT        NOT NULL,
    payment_ts        DATETIME       NOT NULL,
    payment_date      DATE           NOT NULL,
    payment_hour      TINYINT        NOT NULL,
    amount            DECIMAL(12,2)  NOT NULL,
    status            VARCHAR(10)    NOT NULL,
    failure_code      VARCHAR(30)    NULL,
    failure_category  VARCHAR(20)    NULL,
    device_type       VARCHAR(10)    NOT NULL,
    gateway_name      VARCHAR(20)    NOT NULL,
    CONSTRAINT fk_payments_order    FOREIGN KEY (order_id)    REFERENCES orders (order_id),
    CONSTRAINT fk_payments_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT fk_payments_method   FOREIGN KEY (method_id)   REFERENCES payment_methods (method_id),
    CONSTRAINT chk_payments_amount  CHECK (amount > 0),
    CONSTRAINT chk_payments_attempt CHECK (attempt_no BETWEEN 1 AND 5),
    CONSTRAINT chk_payments_status  CHECK (status IN ('SUCCESS','FAILED','PENDING')),
    CONSTRAINT chk_payments_failure CHECK (
        (status = 'FAILED' AND failure_code IS NOT NULL) OR
        (status <> 'FAILED' AND failure_code IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE gateway_transactions (
    gateway_txn_id   BIGINT         NOT NULL PRIMARY KEY,
    payment_id       BIGINT         NOT NULL,
    gateway_name     VARCHAR(20)    NOT NULL,
    gateway_ts       DATETIME       NOT NULL,
    gateway_amount   DECIMAL(12,2)  NOT NULL,
    gateway_status   VARCHAR(10)    NOT NULL,
    response_code    VARCHAR(5)     NOT NULL,
    fee_amount       DECIMAL(10,2)  NOT NULL,
    settlement_date  DATE           NULL,
    CONSTRAINT fk_gateway_payment FOREIGN KEY (payment_id) REFERENCES payments (payment_id),
    CONSTRAINT chk_gateway_amount CHECK (gateway_amount >= 0),
    CONSTRAINT chk_gateway_status CHECK (gateway_status IN ('CAPTURED','DECLINED','TIMEOUT'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE refunds (
    refund_id            BIGINT         NOT NULL PRIMARY KEY,
    payment_id           BIGINT         NOT NULL,
    order_id             BIGINT         NOT NULL,
    refund_amount        DECIMAL(12,2)  NOT NULL,
    refund_reason        VARCHAR(30)    NOT NULL,
    refund_requested_ts  DATETIME       NOT NULL,
    refund_sla_due_ts    DATETIME       NOT NULL,
    refund_status        VARCHAR(10)    NOT NULL,
    refund_processed_ts  DATETIME       NULL,
    CONSTRAINT fk_refunds_payment FOREIGN KEY (payment_id) REFERENCES payments (payment_id),
    CONSTRAINT fk_refunds_order   FOREIGN KEY (order_id)   REFERENCES orders (order_id),
    CONSTRAINT chk_refunds_amount CHECK (refund_amount > 0),
    CONSTRAINT chk_refunds_status CHECK (refund_status IN ('PENDING','PROCESSED','FAILED')),
    CONSTRAINT chk_refunds_reason CHECK (refund_reason IN
        ('CUSTOMER_CANCELLATION','RETURN','DUPLICATE_CHARGE','FAILED_PAYMENT_DEBIT')),
    CONSTRAINT chk_refunds_processed CHECK (
        (refund_status = 'PENDING' AND refund_processed_ts IS NULL) OR
        (refund_status <> 'PENDING' AND refund_processed_ts IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---- indexes (created after the bulk load) ----------------------------
CREATE INDEX idx_orders_date        ON orders (order_date);
CREATE INDEX idx_orders_customer    ON orders (customer_id);
CREATE INDEX idx_orders_status      ON orders (order_status);
CREATE INDEX idx_payments_order     ON payments (order_id);
CREATE INDEX idx_payments_date      ON payments (payment_date);
CREATE INDEX idx_payments_ts        ON payments (payment_ts);
CREATE INDEX idx_payments_method    ON payments (method_id, status);
CREATE INDEX idx_payments_gateway   ON payments (gateway_name, payment_date);
CREATE INDEX idx_payments_customer  ON payments (customer_id);
CREATE INDEX idx_gateway_payment    ON gateway_transactions (payment_id);
CREATE INDEX idx_gateway_status     ON gateway_transactions (gateway_status);
CREATE INDEX idx_refunds_payment    ON refunds (payment_id);
CREATE INDEX idx_refunds_status     ON refunds (refund_status, refund_requested_ts);
