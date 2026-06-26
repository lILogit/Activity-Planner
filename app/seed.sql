-- Initial inventory. Tune freely; this is just a starting graph.

INSERT OR IGNORE INTO activities
  (name, cluster, daylight_required, t_min, t_max, season_mask, typical_duration_min) VALUES
  ('golf',               'SPORT',    1, 10, 30, 'spring,summer,autumn',         210),
  ('cycling',            'SPORT',    1,  8, 28, 'spring,summer,autumn',         120),
  ('swimming',           'SPORT',    0, 18, 35, 'summer',                        90),
  ('cold-water swimming','SPORT',    0, -5, 12, 'autumn,winter,spring',          30),
  ('hike',               'TRIP',     1,  2, 26, 'spring,summer,autumn',         240),
  ('massage',            'RECOVERY', 0,-50, 50, 'spring,summer,autumn,winter',   60),
  ('reading-spot',       'CULTURE',  0,-50, 50, 'spring,summer,autumn,winter',   90);

-- Causal rules (the flattened edges).
-- Rain blocks the outdoor sports; cold boosts cold-water swimming; daylight activates golf.
INSERT OR IGNORE INTO rules (cond_type, cond_value, activity_id, kind, weight)
SELECT 'weather','Rain', id, 'block', 1.0 FROM activities WHERE name IN ('golf','cycling','hike');
INSERT OR IGNORE INTO rules (cond_type, cond_value, activity_id, kind, weight)
SELECT 'weather','Snow', id, 'block', 1.0 FROM activities WHERE name IN ('golf','cycling');
INSERT OR IGNORE INTO rules (cond_type, cond_value, activity_id, kind, weight)
SELECT 'daytime','daylight', id, 'activate', 1.2 FROM activities WHERE name IN ('golf','cycling','hike');
INSERT OR IGNORE INTO rules (cond_type, cond_value, activity_id, kind, weight)
SELECT 'weather','Clear', id, 'activate', 1.3 FROM activities WHERE name = 'golf';
INSERT OR IGNORE INTO rules (cond_type, cond_value, activity_id, kind, weight)
SELECT 'temp','cold', id, 'modulate', 1.5 FROM activities WHERE name = 'cold-water swimming';
INSERT OR IGNORE INTO rules (cond_type, cond_value, activity_id, kind, weight)
SELECT 'season','summer', id, 'block', 1.0 FROM activities WHERE name = 'cold-water swimming';

-- Home anchor (Zbraslav). Excluded from activity matching.
INSERT OR IGNORE INTO venues (name, lat, lon, radius_m, is_anchor)
VALUES ('Home (Neumannova/Faltysova)', 49.9621, 14.3838, 250, 1);

-- A goal: play golf ~3x per fortnight.
INSERT OR IGNORE INTO goals (name, activity_id, metric, target, cadence_days)
SELECT 'golf-improvement', id, 'sessions', 3, 14 FROM activities WHERE name = 'golf';
