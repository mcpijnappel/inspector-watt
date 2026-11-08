-- What woke the DUT: a regex per profile for the firmware's log line that says so; its first group is the reason
-- ("Timer", "GPIO", "Cold boot"). NULL = a generic default (see phases.DEFAULT_WAKE_PATTERN).
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS wake_pattern TEXT;

