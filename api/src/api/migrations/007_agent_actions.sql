-- What agents did to the hardware through MCP (Settings -> Agents shows the last ones).
CREATE TABLE IF NOT EXISTS agent_actions (
    id      BIGSERIAL PRIMARY KEY,
    at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    session TEXT NOT NULL,          -- hash of the MCP session id
    client  TEXT,                   -- e.g. "claude-code 2.1.300"
    host    TEXT,                   -- "this machine" or the LAN address
    tool    TEXT NOT NULL,
    args    JSONB NOT NULL DEFAULT '{}',
    result  TEXT NOT NULL,          -- ok | error | denied
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS agent_actions_at ON agent_actions (at DESC);
