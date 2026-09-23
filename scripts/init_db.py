"""
Run once against Supabase to create the full schema.
Usage: python scripts/init_db.py
"""
import psycopg
from config import DATABASE_URL

DDL = """
-- Clients (contractors / partner companies)
CREATE TABLE IF NOT EXISTS clients (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    contact_name    TEXT,
    contact_email   TEXT,
    contact_phone   TEXT,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Users (staff, admin, partner logins)
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'staff'  CHECK (role IN ('admin','staff','partner')),
    client_id       INT REFERENCES clients(id) ON DELETE SET NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Welders
CREATE TABLE IF NOT EXISTS welders (
    id              SERIAL PRIMARY KEY,
    picture_id      TEXT,
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    email           TEXT,
    mobile          TEXT,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- WPS (Welding Procedure Specifications)
CREATE TABLE IF NOT EXISTS wps (
    id              SERIAL PRIMARY KEY,
    wps_number      TEXT NOT NULL,
    name            TEXT,
    type            TEXT NOT NULL DEFAULT 'WQ'  CHECK (type IN ('WQ','WPQ','PQ','Other')),
    process         TEXT,
    position        TEXT,
    pipe_plate      TEXT,
    size            TEXT,
    thickness       TEXT,
    filler_metal    TEXT,
    price           NUMERIC(10,2),
    cost_code       TEXT,
    client_id       INT REFERENCES clients(id) ON DELETE SET NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Coupon Parts (CPN catalog)
CREATE TABLE IF NOT EXISTS coupon_parts (
    id              SERIAL PRIMARY KEY,
    cpn_number      TEXT NOT NULL,
    description     TEXT,
    material_type   TEXT,
    size            TEXT,
    thickness       TEXT,
    wps_id          INT REFERENCES wps(id) ON DELETE SET NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Test Sessions (one per welder visit)
CREATE TABLE IF NOT EXISTS test_sessions (
    id                  SERIAL PRIMARY KEY,
    session_number      TEXT,
    lab_number          TEXT,
    welder_id           INT NOT NULL REFERENCES welders(id),
    client_id           INT REFERENCES clients(id) ON DELETE SET NULL,
    type                TEXT NOT NULL DEFAULT 'WQ',
    po_number           TEXT,
    job_number          TEXT,
    check_in_datetime   TIMESTAMPTZ,
    check_out_datetime  TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'checked_in'
                            CHECK (status IN ('checked_in','testing','completed','cancelled')),
    added_by            INT REFERENCES users(id) ON DELETE SET NULL,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Test Assignments (individual WPS per session)
CREATE TABLE IF NOT EXISTS test_assignments (
    id              SERIAL PRIMARY KEY,
    session_id      INT NOT NULL REFERENCES test_sessions(id) ON DELETE CASCADE,
    wps_id          INT REFERENCES wps(id) ON DELETE SET NULL,
    cpn_id          INT REFERENCES coupon_parts(id) ON DELETE SET NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','in_progress','completed','cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Lab Results
CREATE TABLE IF NOT EXISTS lab_results (
    id                  SERIAL PRIMARY KEY,
    test_assignment_id  INT NOT NULL REFERENCES test_assignments(id) ON DELETE CASCADE,
    vt_root             TEXT,
    vt_cap              TEXT,
    lab_result          TEXT,
    final_result        TEXT,
    recorded_by         INT REFERENCES users(id) ON DELETE SET NULL,
    recorded_at         TIMESTAMPTZ,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Invitations (pre-scheduled welder visits)
CREATE TABLE IF NOT EXISTS invitations (
    id                  SERIAL PRIMARY KEY,
    invitation_number   TEXT NOT NULL,
    client_id           INT REFERENCES clients(id) ON DELETE SET NULL,
    expected_date       DATE,
    notes               TEXT,
    created_by          INT REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Visitors (non-welder check-ins)
CREATE TABLE IF NOT EXISTS visitors (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL,
    email               TEXT,
    company             TEXT,
    visiting_person     TEXT,
    check_in_datetime   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    check_out_datetime  TIMESTAMPTZ
);

-- Muster Zones
CREATE TABLE IF NOT EXISTS muster_zones (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL,
    active  BOOLEAN NOT NULL DEFAULT TRUE
);

-- Muster Assignments
CREATE TABLE IF NOT EXISTS muster_assignments (
    id          SERIAL PRIMARY KEY,
    session_id  INT NOT NULL REFERENCES test_sessions(id) ON DELETE CASCADE,
    zone_id     INT NOT NULL REFERENCES muster_zones(id) ON DELETE CASCADE,
    UNIQUE(session_id)
);

-- Webhook Events (audit log)
CREATE TABLE IF NOT EXISTS webhook_events (
    id              SERIAL PRIMARY KEY,
    event_id        TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    session_id      INT REFERENCES test_sessions(id) ON DELETE SET NULL,
    payload         JSONB,
    endpoint_url    TEXT,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    response_code   INT,
    response_body   TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('sent','failed','pending'))
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_test_sessions_welder    ON test_sessions(welder_id);
CREATE INDEX IF NOT EXISTS idx_test_sessions_client    ON test_sessions(client_id);
CREATE INDEX IF NOT EXISTS idx_test_sessions_status    ON test_sessions(status);
CREATE INDEX IF NOT EXISTS idx_test_sessions_date      ON test_sessions(check_in_datetime);
CREATE INDEX IF NOT EXISTS idx_test_assignments_sess   ON test_assignments(session_id);
CREATE INDEX IF NOT EXISTS idx_welders_picture_id      ON welders(picture_id);
CREATE INDEX IF NOT EXISTS idx_webhook_events_session  ON webhook_events(session_id);

-- Seed default muster zones
INSERT INTO muster_zones (name) VALUES
    ('Zone A'), ('Zone B'), ('Zone C'), ('Zone D')
ON CONFLICT DO NOTHING;
"""

if __name__ == '__main__':
    print('Connecting to database...')
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    print('Schema created successfully.')
