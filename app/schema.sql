-- KAIROS schema. The "graph" is flattened: nodes are tables, edges live in `rules`.
PRAGMA journal_mode = WAL;

-- Raw GPS log (one row per Overland location). Mirrors the old N8N Records table.
CREATE TABLE IF NOT EXISTS pings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,            -- epoch seconds
    lat           REAL    NOT NULL,
    lon           REAL    NOT NULL,
    altitude      REAL,
    wifi          TEXT,
    address       TEXT,
    weather_main  TEXT,
    weather_desc  TEXT,
    temp          REAL,
    pressure      REAL,
    humidity      REAL,
    wind_speed    REAL,
    distance_m    REAL,                        -- to previous ping
    duration_s    REAL,                        -- since previous ping
    -- No FK: the sentinel value 0 marks anchor-suppressed pings (home dwells
    -- that must never become a session), and NULL means still-unassigned. A
    -- REFERENCES sessions(id) would reject the sentinel-0 write.
    session_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pings_ts ON pings(ts);

-- Activity inventory. alpha/beta = Beta posterior over the star rating.
CREATE TABLE IF NOT EXISTS activities (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT UNIQUE NOT NULL,
    cluster               TEXT NOT NULL,       -- SPORT|TRIP|RECOVERY|CULTURE|SOCIAL
    alpha                 REAL NOT NULL DEFAULT 1.0,
    beta                  REAL NOT NULL DEFAULT 1.0,
    daylight_required     INTEGER NOT NULL DEFAULT 0,
    t_min                 REAL,                -- preferred temp range (Celsius)
    t_max                 REAL,
    season_mask           TEXT NOT NULL DEFAULT 'spring,summer,autumn,winter',
    typical_duration_min  INTEGER NOT NULL DEFAULT 90,
    active                INTEGER NOT NULL DEFAULT 1   -- 0 = ARCHIVED
);

-- Venues afford activities. Anchors (home) are excluded from matching.
CREATE TABLE IF NOT EXISTS venues (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    radius_m     REAL NOT NULL DEFAULT 200,
    activity_id  INTEGER REFERENCES activities(id),
    is_anchor    INTEGER NOT NULL DEFAULT 0,
    address      TEXT                          -- LocationIQ reverse-geocode of lat/lon
);

-- The causal "edges", flattened. kind: activate | block | modulate.
-- A SQL join over (cond_type, cond_value) replaces graph traversal.
CREATE TABLE IF NOT EXISTS rules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cond_type    TEXT NOT NULL,               -- weather|season|daytime|temp|health
    cond_value   TEXT NOT NULL,               -- e.g. 'Rain', 'winter', 'daylight'
    activity_id  INTEGER NOT NULL REFERENCES activities(id),
    kind         TEXT NOT NULL,               -- activate|block|modulate
    weight       REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_rules_cond ON rules(cond_type, cond_value);

-- One detected & classified staypoint = one session.
CREATE TABLE IF NOT EXISTS sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts           INTEGER NOT NULL,
    end_ts             INTEGER NOT NULL,
    duration_s         REAL NOT NULL,
    centroid_lat       REAL NOT NULL,
    centroid_lon       REAL NOT NULL,
    venue_id           INTEGER REFERENCES venues(id),
    activity_id        INTEGER REFERENCES activities(id),
    confidence         REAL,
    feedback_requested INTEGER NOT NULL DEFAULT 0,
    venue_pending      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    stars       INTEGER NOT NULL,            -- 1..5
    note        TEXT,
    emotion     TEXT,
    ts          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT UNIQUE NOT NULL,
    activity_id  INTEGER REFERENCES activities(id),
    metric       TEXT NOT NULL DEFAULT 'sessions',
    target       REAL NOT NULL,
    cadence_days INTEGER NOT NULL DEFAULT 14,
    progress     REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,                -- YYYY-MM-DD
    horizon     TEXT NOT NULL,                -- daily|weekly
    status      TEXT NOT NULL DEFAULT 'proposed', -- proposed|approved|rejected|executed
    payload     TEXT NOT NULL,               -- JSON ranked picks + reasoning
    created_ts  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS patterns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    from_venue_id    INTEGER,                 -- origin venue
    to_venue_id      INTEGER,                 -- destination venue
    from_lat         REAL NOT NULL,
    from_lon         REAL NOT NULL,
    to_lat           REAL NOT NULL,
    to_lon           REAL NOT NULL,
    count            INTEGER NOT NULL,         -- times this route was taken
    typical_hour     INTEGER NOT NULL,         -- modal hour (0-23)
    first_seen       INTEGER NOT NULL,         -- epoch seconds
    last_seen        INTEGER NOT NULL,         -- epoch seconds
    kind             TEXT NOT NULL DEFAULT 'routine'  -- routine | commute | trip
);
