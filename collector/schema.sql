-- C-You Occupancy Monitoring Schema

-- Offices
CREATE TABLE IF NOT EXISTS offices (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    location TEXT,
    floor_plan_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Access Points
CREATE TABLE IF NOT EXISTS access_points (
    id SERIAL PRIMARY KEY,
    office_id INTEGER REFERENCES offices(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    ip_address INET NOT NULL UNIQUE,
    ssh_user TEXT NOT NULL,
    ssh_password TEXT NOT NULL,
    mac_address MACADDR,
    model TEXT DEFAULT 'U7 Pro Max',
    -- Position on floor plan (0-1 normalized coordinates)
    map_x REAL,
    map_y REAL,
    -- Listener state
    listener_status TEXT DEFAULT 'unknown',  -- unknown, deployed, unreachable, stale
    listener_last_seen TIMESTAMPTZ,
    listener_server_ip INET,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Spectral readings hypertable
CREATE TABLE IF NOT EXISTS spectral_readings (
    time TIMESTAMPTZ NOT NULL,
    ap_id INTEGER NOT NULL REFERENCES access_points(id),
    freq INTEGER NOT NULL,
    noise_floor SMALLINT,
    rssi SMALLINT,
    max_scale SMALLINT,
    max_mag SMALLINT,
    tsf BIGINT,
    nonzero_bins INTEGER,
    max_bin_val SMALLINT,
    max_bin_idx SMALLINT,
    radio TEXT  -- '2.4ghz', '5ghz', '6ghz'
);

SELECT create_hypertable('spectral_readings', 'time', if_not_exists => TRUE);

-- Compress after 1 day (office data is high volume, less need for raw history)
ALTER TABLE spectral_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'ap_id,radio',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('spectral_readings', INTERVAL '1 day', if_not_exists => TRUE);

-- Occupancy state per AP
CREATE TABLE IF NOT EXISTS ap_occupancy (
    time TIMESTAMPTZ NOT NULL,
    ap_id INTEGER NOT NULL REFERENCES access_points(id),
    intensity REAL NOT NULL DEFAULT 0,  -- 0.0 (empty) to 1.0 (heavy presence)
    radio TEXT,
    metadata JSONB
);

SELECT create_hypertable('ap_occupancy', 'time', if_not_exists => TRUE);

-- Office-level occupancy (aggregated from APs)
CREATE TABLE IF NOT EXISTS office_occupancy (
    time TIMESTAMPTZ NOT NULL,
    office_id INTEGER NOT NULL REFERENCES offices(id),
    occupied BOOLEAN NOT NULL DEFAULT FALSE,
    ap_count INTEGER,
    active_ap_count INTEGER,
    avg_intensity REAL
);

SELECT create_hypertable('office_occupancy', 'time', if_not_exists => TRUE);

-- AP health log
CREATE TABLE IF NOT EXISTS ap_health_log (
    time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ap_id INTEGER NOT NULL REFERENCES access_points(id),
    status TEXT NOT NULL,  -- healthy, unreachable, redeployed, ssh_failed, deploy_failed
    details TEXT
);

SELECT create_hypertable('ap_health_log', 'time', if_not_exists => TRUE);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_spectral_ap_time ON spectral_readings (ap_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_occupancy_ap_time ON ap_occupancy (ap_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_office_occ_time ON office_occupancy (office_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_ap_office ON access_points (office_id);
