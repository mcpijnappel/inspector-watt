-- The default phase profile: phases from plain log lines any firmware can print. The lines are documented in
-- docs/firmware-logging.md (keep the two in step; api/tests/test_default_profile.py checks them). Case doesn't
-- matter and any log prefix may come first. Boot starts a cycle: the ESP32's own reset line "rst:0x..", or
-- "Booting" (Zephyr prints "*** Booting Zephyr OS ***" by itself).
INSERT INTO profiles (name, rules, types) VALUES ('Default', '[
  {"name": "Boot", "color": "#a371f7", "match": "regex", "pattern": "(?i)rst:0x|\\bbooting\\b", "snap": false},
  {"name": "Measure", "color": "#d29922", "match": "regex", "pattern": "(?i)\\bmeasuring\\b", "snap": false},
  {"name": "Connect", "color": "#58a6ff", "match": "regex", "pattern": "(?i)\\bconnecting\\b", "snap": false},
  {"name": "Upload", "color": "#3fb950", "match": "regex", "pattern": "(?i)\\b(?:uploading|sending|publishing)\\b", "snap": false},
  {"name": "Sleep", "color": "#8b949e", "match": "regex", "pattern": "(?i)\\b(?:going to|entering)(?: deep| light)? sleep\\b", "snap": true}
]', '[
  {"name": "Measure only", "begin": "Boot", "end": null, "must": [], "exclude": ["Connect"]},
  {"name": "With upload", "begin": "Boot", "end": null, "must": ["Connect"], "exclude": []},
  {"name": "Upload to upload", "begin": "Connect", "end": null, "must": [], "exclude": []},
  {"name": "Connect + upload", "begin": "Connect", "end": {"phase": "Upload", "include": true}, "must": [], "exclude": []}
]') ON CONFLICT (name) DO NOTHING;

UPDATE devices SET profile_id = (SELECT id FROM profiles WHERE name = 'Default') WHERE profile_id IS NULL;
UPDATE sessions SET profile_id = (SELECT id FROM profiles WHERE name = 'Default') WHERE profile_id IS NULL;
