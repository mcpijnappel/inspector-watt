// Typed access to the api service (/api/...) and, through it, the capture service (/api/device/...).

export type Ppk2Status = {
  port: string;
  connected: boolean;
  streaming: boolean;
  error: string | null;
  mode: 'source' | 'ampere' | null;
  voltage_mv: number | null;
  voltage_set_by_us: boolean;
  dut_power: boolean | null;
  /** Last board power change: who did it (user, session start/stop, idle auto-off) and when (unix s). */
  dut_changed: { on: boolean; by: 'user' | 'session' | 'idle' | 'limit' | 'flash' | 'agent'; at: number } | null;
  spike_filter: boolean;
  /** Effective limit: the device's maximum, capped by the tracker's limit (hard_max_mv, a setting). */
  max_voltage_mv: number;
  hard_max_mv: number;
  device: { id: number; name: string; max_mv: number } | null;
  stream: {
    samples: number;
    lost: number;
    gaps: number;
    stalls: number;
    reconnects: number;
    elapsed_s: number | null;
    rate_sps: number | null;
    nominal_rate_sps?: number;
    corrupt?: number;
    dropped_bytes?: number;
    decode_load: number | null;
    clock_drift_ppm: number | null;
  };
};

export type UartStatus = { port: string; connected: boolean; error: string | null; baud: number; bytes_read: number; lines: number };

export type ActiveSession = {
  id: number;
  name: string;
  kind: 'detail' | 'soak';
  started_at: string;
  config: Record<string, unknown>;
  s0: number;
  processed_samples: number;
  duration_s: number;
  lag_s: number;
  charge_c: number;
  awake: boolean;
  wakes: number;
  segments: number;
  raw_bytes: number;
  low_disk: boolean;
  gaps: number;
  pending_rows: number;
  battery?: BatteryStatus | null;
};

export type SessionStatus = { active: boolean; db_ok: boolean; error: string | null; session?: ActiveSession };

/** The board's own USB port (console + flashing). Closed unless someone opens the console. */
export type UsbStatus = {
  port: string;
  /** The port exists: the board is awake (it disappears in deep sleep). */
  present: boolean;
  /** Someone has the console open (the board can't deep-sleep then). */
  wanted: boolean;
  connected: boolean;
  /** Flashing: esptool has the port. */
  held: boolean;
  error: string | null;
  opened_at: number | null;
  lines: number;
  /** A key menu the firmware printed (e.g. its debug menu), waiting for a key. */
  menu: { title: string; options: { key: string; label: string }[]; at: number; prompt: string } | null;
};

export type FlashQuestion = { kind: 'recording'; session_id: number; session_name: string; options: { choice: FlashChoice; label: string }[] };
export type FlashChoice = 'keep' | 'stop' | 'after' | 'cancel';
export type FlashJob = {
  id: number;
  status: 'queued' | 'waiting_choice' | 'waiting_recording' | 'waiting_wake' | 'preparing' | 'waking' | 'flashing' | 'done' | 'failed' | 'cancelled';
  created: number;
  finished: number | null;
  progress: number;
  error: string | null;
  question: FlashQuestion | null;
  choice: FlashChoice | null;
  /** The recording that got a 'flash' event. */
  session_id: number | null;
  power_cycled: boolean;
  meta: { project?: string; env?: string; version?: string; git?: string; host?: string; source?: string };
  chip: string;
  /** Which backend flashed it: esp32 (esptool) or rp2 (picotool). */
  family: 'esp32' | 'rp2' | string;
  /** What powers the board: the PPK2 (woken / power-cycled for the flash) or its own USB (just plugged in). */
  power: 'ppk2' | 'usb';
  /** The chip's MAC as esptool read it (hex, no colons): which board got flashed. */
  mac: string | null;
  /** Written even if already on the board (no --skip-flashed). */
  full_flash: boolean;
  /** skipped: already on the board, not written (null: not flashed yet). */
  images: { offset: number | null; bytes: number; skipped: boolean | null }[];
  bytes: number;
  /** PPK2 stream indices of the flash (live chart shading). */
  start_idx: number | null;
  end_idx: number | null;
  /** waiting_wake: when it power-cycles the board instead (unix time). */
  wait_until: number | null;
  /** waiting_recording: the recording it waits for. */
  waiting_for: string | null;
  when_asleep: 'wait' | 'always' | 'cycle' | null;
  log_total: number;
  log?: string[];
};

export type Status = {
  ppk2: Ppk2Status;
  uart: UartStatus;
  usb?: UsbStatus;
  /** The running or last flash job, without its log. */
  flash?: FlashJob | null;
  buffer: { generation: number; start_idx: number; end_idx: number; end_wall: number | null; sample_period_s: number; capacity: number };
  session: SessionStatus | null;
  /** Board switches off after `after_s` without a viewer while nothing records; in_s = null: it won't. */
  idle_power_off: { after_s: number; in_s: number | null };
  time: number;
};

export type SessionForm = {
  name: string;
  kind: 'detail' | 'soak';
  notes: string;
  voltage_mv: number | null;
  dut_power: boolean;
  wake_threshold_ua: number;
  hold_ms: number;
  pre_ms: number;
  post_ms: number;
  raw_buffer_hours: number;
  auto_restore: boolean;
  uart_baud: number | null;
  max_duration_s: number | null;
  /** Battery emulation instead of a fixed voltage (null = fixed). */
  battery?: BatteryForm | null;
};

export type BatteryForm = {
  mode: 'charge' | 'time';
  chemistry: string;
  cells: number;
  capacity_mah: number;
  speedup: number;
  start_soc: number;
  start_mv: number;
  sweep_min: number;
  cutoff_mv: number;
  on_cutoff: 'protect' | 'hunt';
  floor_mv: number;
};

/** A device (board) on the bench: its defaults, its voltage limit and the phase rules new sessions get. */
export type DeviceForm = {
  name: string;
  notes: string;
  voltage_mv: number;
  max_mv: number;
  wake_threshold_ua: number;
  /** Its debug UART's speed: the UART runs at it while this device is on the bench. */
  uart_baud: number;
  profile_id: number | null;
};
export type Device = DeviceForm & { id: number; profile_name?: string | null; sessions?: number };

/** A battery type the emulation knows: curve per cell as [state of charge %, mV], empty to full. */
export type Chemistry = { id: string; label: string; note: string; cells: number[]; cutoff_mv: number; floor_mv: number; curve: [number, number][] };

/** A running battery-emulation session: its settings plus where the battery is now. */
export type BatteryStatus = BatteryForm & { voltage_mv: number; soc: number | null; eta_s: number | null; below_cutoff: boolean };

export type Preset = { id: number; name: string; config: Partial<SessionForm> };

export type UartLine = { seq: number; wall: number; sample_idx: number | null; text: string };

export type BufferWindow = {
  generation: number;
  start: number;
  bin_samples: number;
  current?: (number | null)[];
  min?: (number | null)[];
  max?: (number | null)[];
  mean?: (number | null)[];
};

export type RangeStats = {
  start: number;
  end: number;
  duration_s: number;
  mean_a: number | null;
  min_a: number | null;
  max_a: number | null;
  charge_c: number;
  lost: number;
};

export type Batteries = { sizes_mah: number[]; selected_mah: number };

export type SessionSummary = {
  id: number;
  name: string;
  kind: 'detail' | 'soak';
  status: 'running' | 'stopped' | 'interrupted';
  started_at: string;
  stopped_at: string | null;
  n_samples: number;
  charge_c: number;
  config: Record<string, unknown>;
  notes: string;
  duration_s: number;
  avg_current_a: number | null;
  profile_id?: number | null;
  device_id?: number | null;
  device_name?: string | null;
  wakes?: number;
  raw_bytes?: number;
};

/** Unexpected board resets of one kind with the same log line before them (numbers ignored). */
export type ResetGroup = {
  kind: string;
  /** "brownout", "watchdog", "crash", "software reset", "power lost", or the chip's own name. */
  label: string;
  chip: string;
  /** The last log line in the 10 s before the reset (why it restarted, e.g. "Rebooting..." after an update). */
  explanation: string | null;
  panic: string | null;
  count: number;
  first_t: number;
  times: number[];
};
export type SessionDetail = SessionSummary & {
  events: Record<string, number>;
  raw: Record<string, { files: number; bytes: number; seconds: number }>;
  uart_lines: number;
  resets: { count: number; groups: ResetGroup[] };
};

export type Trace = {
  tier: 'raw' | '10ms' | '1s' | '1m' | '1h';
  bucket_s: number;
  source: 'parquet' | 'live' | 'db';
  raw_available?: boolean;
  coverage?: number;
  start_s: number;
  end_s: number;
  x: number[];
  min: (number | null)[];
  max: (number | null)[];
  mean: (number | null)[];
};

export type SessionStats = {
  start_s: number;
  end_s: number;
  duration_s: number;
  mean_a: number | null;
  charge_c: number;
  min_a: number | null;
  max_a: number | null;
  coverage: number;
  exact: boolean;
  wakes: { n: number; avg_duration_s: number | null; avg_charge_c: number | null; max_peak_a: number | null; charge_c: number | null; charge_share: number };
};

export type SessionEvent = {
  /** A number, or "reset-<n>" for a reset found in the serial log. */
  id: number | string;
  kind: string;
  t: number;
  end_t: number | null;
  peak_a: number | null;
  mean_a: number | null;
  charge_c: number | null;
  data: Record<string, unknown>;
};

export type SessionUartLine = { t: number; sample_idx: number | null; wall: string; text: string };

export type Rule = { name: string; color: string; match: 'contains' | 'regex'; pattern: string; snap: boolean };
/** A cycle type: begins at a phase, optionally ends at one (before it or including it), must have / may not have
 *  some phases. Phases by name. */
export type CycleType = {
  name: string; begin: string; end: { phase: string; include: boolean } | null; must: string[]; exclude: string[];
  /** Only cycles that begin in a wake with one of these wake reasons. */
  woken_by?: string[];
};
/** wake_pattern: regex for the log line that says what woke the board (first group = the reason); null = default. */
export type Profile = { id: number; name: string; rules: Rule[]; types: CycleType[]; wake_pattern?: string | null; updated_at?: string };
/** A cycle type's cycles in a session: [start, end) indices into all phases of all cycles, in order. */
export type TypeResult = {
  name: string;
  kind: 'wake' | 'period' | 'stretch';
  instances: [number, number][];
  filtered: number;
  no_end: number;
  gaps: number;
  error: string | null;
};

/** [rule, t, end_t, charge_c, mean_a, peak_a, coverage] */
export type PhaseRow = [number, number, number, number, number | null, number | null, number];
export type CycleRow = { i: number; t: number; end_t: number; closed: boolean; complete: boolean; charge_c: number; mean_a: number | null; phases: PhaseRow[]; wake?: string | null };
/** One phase averaged over a type's usable cycles: duration and mean current over the cycles that have it
 *  (in_cycles), charge per cycle over all of them, so the rows add up. */
export type PhaseAvg = {
  phase: string; rule: number; in_cycles: number; duration_s: number | null; mean_a: number | null;
  charge_per_cycle_c: number; share: number | null; times_per_cycle?: number; peak_a: number | null;
};
type Spread = { mean: number | null; typical: number | null; p10: number | null; p90: number | null };
/** A cycle type's averages, worked out by the api (the same numbers the MCP tools give). */
export type TypeSummary = {
  type: string; kind: 'wake' | 'period' | 'stretch'; error: string | null;
  cycles: number; usable: number; duration_s: Spread; charge_c: Spread; mean_a: number | null;
  /** The cycle with the median charge (index within the type). */
  typical_index: number | null;
  /** What woke its usable cycles: reason -> count, most first. */
  woken_by: Record<string, number>;
  phases: PhaseAvg[];
};
export type PhasesResult = {
  profile: { id: number; name: string; rules: Rule[]; types: CycleType[]; wake_pattern?: string | null } | null;
  cycles: CycleRow[];
  types: TypeResult[];
  /** All wakes first, then the profile's types in order. */
  stats: TypeSummary[];
  /** The sleep phase (lowest average current over all wakes); -1 when there is none. */
  sleep_rule: number;
};
/** Battery life from the measured mix of wakes, and a what-if. */
type Life = { mean_a: number; hours: number; days: number } | null;
export type Battery = {
  measured: Life;
  what_if: Life;
  mix: { type: string; wakes: number; one_in: number | null; charge_c: number | null }[];
  sleep: { phase: string | null; rule: number; mean_a: number; duration_s: number | null };
};
export type BatteryWhatIf = { sleep_s?: number | null; without?: string[]; type?: string | null; every?: number | null };

/** Capture settings: changeable while running, and the ones fixed in .env. */
export type Measurement = {
  idle_power_off_s: number;
  spike_filter: boolean;
  /** Start measuring when the PPK2 connects. */
  auto_stream: boolean;
  /** The tracker's voltage limit (mV): changed only through setMaxVoltage, never by agents. */
  max_voltage_mv: number;
  /** Stop writing raw data below this much free disk. */
  min_free_gb: number;
  /** Raw precision of new recordings (23 = lossless). */
  raw_mantissa_bits: number;
  /** Uploads to a sleeping board: wait for its next wake (power-cycle after flash_wake_wait_s) or power-cycle now. */
  flash_when_asleep: 'wait' | 'always' | 'cycle';
  flash_wake_wait_s: number;
  fixed: { ring_seconds: number; ppk2_port: string; uart_port: string; usb_port: string };
};
/** A module's setting, as it describes it (the form is drawn from this). */
export type ModuleSetting = {
  key: string;
  type: 'choice' | 'number' | 'bool' | 'text';
  label: string;
  hint?: string;
  unit?: string;
  default: unknown;
  choices?: (number | string)[];
  /** Settings of one group (e.g. "wiring") are shown together on one row. */
  group?: string;
  min?: number;
  max?: number;
};
/** A module (modules/<name>/): installed or not, and what it says about itself. */
export type ModuleInfo = {
  name: string;
  title: string;
  installed: boolean;
  error: string | null;
  module: {
    name: string; title: string; version: string; contract: number; description?: string;
    health: { ok: boolean; detail: string | null };
    settings: ModuleSetting[];
    events: { kind: string; label: string; draw: string; color: string }[];
    tools: { name: string; description: string; hardware: boolean }[];
  } | null;
  values: Record<string, unknown>;
};
export type StorageRow = { id: number; name: string; kind: string; status: string; started_at: string; duration_s: number; raw_bytes: number; raw_files: number; raw_seconds: number; raw_kinds: string[] };
export type Storage = { disk: { total_bytes: number; free_bytes: number; used_bytes: number }; database_bytes: number | null; raw_bytes: number; min_free_gb: number | null; sessions: StorageRow[] };
export type AgentRequest = {
  id: number;
  /** Hash of the agent's MCP session id. */
  session: string;
  client: string;
  host: string;
  via: 'local' | 'lan';
  /** The hardware call it wants to make first. */
  tool: string;
  args: Record<string, unknown>;
  at: number;
  status: 'pending' | 'allowed' | 'denied' | 'expired';
  expires_at: number | null;
};
export type AgentGrant = { session: string; client: string; host: string; via: 'local' | 'lan'; since: number; until: number; actions: number; last_action: string | null };
export type AgentState = { hardware: boolean; lan: boolean; pending: AgentRequest[]; grants: AgentGrant[]; max_grant_s: number; ask_wait_s: number };
export type AgentAction = { id: number; at: string; session: string; client: string | null; host: string | null; tool: string; args: Record<string, unknown>; result: 'ok' | 'error' | 'denied'; detail: string | null };

export type McpToken = { id: number; name: string; token: string; created: string | null };
export type McpStatus = { lan: boolean; hardware: boolean; tokens: McpToken[]; server: { up: boolean; error?: string; tools?: { name: string; description: string }[] } };
export type Health = { db: boolean; capture: boolean; mcp: boolean };

export class ApiError extends Error {}

async function req<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const r = await fetch(path, {
    method,
    signal,
    headers: body === undefined ? undefined : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const j = await r.json();
      if (typeof j.detail === 'string') msg = j.detail;
      else if (Array.isArray(j.detail)) msg = j.detail.map((d: { loc?: unknown[]; msg: string }) => `${d.loc?.at(-1) ?? ''}: ${d.msg}`).join(', ');
    } catch {
      /* not JSON */
    }
    throw new ApiError(msg);
  }
  return r.json() as Promise<T>;
}

export const api = {
  /** viewer: this poll comes from an open browser, which keeps the board from switching off when idle. */
  status: (viewer = false) => req<Status>('GET', `/api/device/status${viewer ? '?viewer=true' : ''}`),
  setSource: (voltage_mv: number) => req<Ppk2Status>('POST', '/api/device/ppk2/source', { voltage_mv }),
  setDut: (on: boolean) => req<Ppk2Status>('POST', '/api/device/ppk2/dut', { on }),
  setStream: (on: boolean) => req<Ppk2Status>('POST', '/api/device/ppk2/stream', { on }),
  chemistries: () => req<Chemistry[]>('GET', '/api/device/battery/chemistries'),
  setBaud: (baud: number) => req<UartStatus>('PUT', '/api/device/uart/config', { baud }),
  window: (start: number, end: number, points: number) =>
    req<BufferWindow>('GET', `/api/device/buffer/window?start=${start}&end=${end}&points=${points}`),
  uartLines: (afterSeq: number, limit: number) =>
    req<UartLine[]>('GET', `/api/device/uart/lines?after_seq=${afterSeq}&limit=${limit}`),
  rangeStats: (start: number, end: number) => req<RangeStats>('GET', `/api/device/buffer/stats?start=${start}&end=${end}`),
  saveBuffer: (name: string, start_idx: number, end_idx: number, notes = '') =>
    req<{ saved: number; duration_s: number }>('POST', '/api/device/sessions/save-buffer', { name, start_idx, end_idx, notes }),
  getSetting: <T>(key: string) => req<T | null>('GET', `/api/settings/${key}`),
  putSetting: <T>(key: string, value: T) => req<T>('PUT', `/api/settings/${key}`, { value }),

  startSession: (form: SessionForm) => req<SessionStatus>('POST', '/api/sessions', form),
  stopSession: (dut_off = false) => req<{ stopped: number }>('POST', '/api/sessions/stop', { dut_off }),
  lastConfig: () => req<{ name: string | null; config: Partial<SessionForm> } | null>('GET', '/api/last-session-config'),

  sessions: () => req<SessionSummary[]>('GET', '/api/sessions'),
  session: (id: number) => req<SessionDetail>('GET', `/api/sessions/${id}`),
  patchSession: (id: number, patch: { name?: string; notes?: string }) => req<SessionSummary>('PATCH', `/api/sessions/${id}`, patch),
  deleteSession: (id: number) => req<{ deleted: number }>('DELETE', `/api/sessions/${id}`),
  trace: (id: number, start: number, end: number, points: number) =>
    req<Trace>('GET', `/api/sessions/${id}/trace?start_s=${start}&end_s=${end}&points=${points}`),
  sessionStats: (id: number, start?: number, end?: number) =>
    req<SessionStats>('GET', `/api/sessions/${id}/stats${start == null ? '' : `?start_s=${start}&end_s=${end}`}`),
  events: (id: number, kind?: string) => req<SessionEvent[]>('GET', `/api/sessions/${id}/events?limit=20000${kind ? `&kind=${kind}` : ''}`),
  sessionUart: (id: number, start: number, end: number, limit = 2000, q = '') =>
    req<SessionUartLine[]>('GET', `/api/sessions/${id}/uart?start_s=${start}&end_s=${end}&limit=${limit}${q ? `&q=${encodeURIComponent(q)}` : ''}`),

  phases: (id: number, profileId?: number) => req<PhasesResult>('GET', `/api/sessions/${id}/phases${profileId ? `?profile_id=${profileId}` : ''}`),
  battery: (id: number, w: BatteryWhatIf = {}, signal?: AbortSignal) => {
    const p = new URLSearchParams();
    if (w.sleep_s != null) p.set('sleep_s', String(w.sleep_s));
    for (const n of w.without ?? []) p.append('without', n);
    if (w.type != null && w.every != null) {
      p.set('type', w.type);
      p.set('every', String(w.every));
    }
    return req<Battery>('GET', `/api/sessions/${id}/battery?${p}`, undefined, signal);
  },
  testRules: (id: number, rules: Rule[], wake_pattern?: string | null) =>
    req<{ cycles: number; found: number[]; woken_by: Record<string, number> }>('POST', `/api/sessions/${id}/phases/test`, { rules, wake_pattern: wake_pattern || null }),
  testTypes: (id: number, types: CycleType[]) => req<TypeResult[]>('POST', `/api/sessions/${id}/types/test`, { types }),
  devices: () => req<Device[]>('GET', '/api/devices'),
  createDevice: (d: DeviceForm) => req<Device>('POST', '/api/devices', d),
  updateDevice: (id: number, d: DeviceForm) => req<Device>('PUT', `/api/devices/${id}`, d),
  deleteDevice: (id: number) => req<{ deleted: number }>('DELETE', `/api/devices/${id}`),
  activeDevice: () => req<Device | null>('GET', '/api/devices/active'),
  setActiveDevice: (id: number | null) => req<Device | null>('PUT', '/api/devices/active', { id }),
  profiles: () => req<Profile[]>('GET', '/api/profiles'),
  createProfile: (name: string, rules: Rule[], types: CycleType[] = [], wake_pattern = '') => req<Profile>('POST', '/api/profiles', { name, rules, types, wake_pattern }),
  updateProfile: (id: number, patch: { name?: string; rules?: Rule[]; types?: CycleType[]; wake_pattern?: string }) => req<Profile>('PUT', `/api/profiles/${id}`, patch),
  deleteProfile: (id: number) => req<{ deleted: number }>('DELETE', `/api/profiles/${id}`),
  setSessionProfile: (id: number, profile_id: number) => req<SessionSummary>('PATCH', `/api/sessions/${id}`, { profile_id }),

  measurement: () => req<Measurement>('GET', '/api/measurement'),
  setMeasurement: (m: { idle_power_off_s?: number; spike_filter?: boolean; flash_when_asleep?: 'wait' | 'always' | 'cycle'; flash_wake_wait_s?: number;
    auto_stream?: boolean; min_free_gb?: number; raw_mantissa_bits?: number }) => req<Measurement>('PUT', '/api/measurement', m),
  /** The tracker's voltage limit: the UI asks for a confirmation first; the api refuses it to agents. */
  setMaxVoltage: (mv: number) => req<Measurement>('PUT', '/api/measurement/max-voltage', { max_voltage_mv: mv }),
  storage: () => req<Storage>('GET', '/api/storage'),
  modules: () => req<ModuleInfo[]>('GET', '/api/modules'),
  setModuleSettings: (name: string, values: Record<string, unknown>) => req<Record<string, unknown>>('PUT', `/api/modules/${name}/settings`, values),
  /** A module's latest events (Live), after the last one seen. */
  moduleLiveEvents: <T>(name: string, afterSeq: number) => req<T[]>('GET', `/api/modules/${name}/events/live?after_seq=${afterSeq}&limit=2000`),
  /** The logic analyzer: decode the last seconds of its buffer (the transactions also land in the Live list). */
  analyzerDecode: <T>(seconds: number) => req<T>('POST', '/api/modules/analyzer/decode', { seconds }),
  mcp: () => req<McpStatus>('GET', '/api/mcp'),
  setMcpLan: (lan: boolean) => req<McpStatus>('PUT', '/api/mcp', { lan }),
  setMcpHardware: (hardware: boolean) => req<McpStatus>('PUT', '/api/mcp', { hardware }),
  agents: () => req<AgentState>('GET', '/api/agents'),
  agentAnswer: (id: number, allow: boolean) => req<AgentRequest>('POST', `/api/agents/requests/${id}/answer`, { allow }),
  agentRevoke: (session: string | null = null) => req<{ revoked: number }>('POST', '/api/agents/revoke', { session }),
  agentActions: (limit = 50) => req<AgentAction[]>('GET', `/api/agents/actions?limit=${limit}`),
  addMcpToken: (name: string) => req<McpStatus>('POST', '/api/mcp/tokens', { name }),
  renewMcpToken: (id: number) => req<McpStatus>('POST', `/api/mcp/tokens/${id}/renew`),
  deleteMcpToken: (id: number) => req<McpStatus>('DELETE', `/api/mcp/tokens/${id}`),
  health: () => req<Health>('GET', '/api/health'),
  ppk2Metadata: () => req<{ raw: string; parsed: Record<string, unknown> }>('GET', '/api/device/ppk2/metadata'),

  usb: () => req<UsbStatus>('GET', '/api/device/usb'),
  usbOpen: (on: boolean) => req<UsbStatus>('POST', '/api/device/usb/open', { on }),
  usbLines: (afterSeq: number, limit = 1000) => req<UartLine[]>('GET', `/api/device/usb/lines?after_seq=${afterSeq}&limit=${limit}`),
  usbSend: (text: string) => req<{ sent_bytes: number }>('POST', '/api/device/usb/send', { text, eol: '\n' }),
  usbKey: (key: string) => req<{ sent_bytes: number }>('POST', '/api/device/usb/key', { key }),
  flashJob: (id: number, after = 0) => req<FlashJob>('GET', `/api/flash/${id}?after=${after}`),
  flashJobs: () => req<FlashJob[]>('GET', '/api/flash/jobs'),
  flashAnswer: (id: number, choice: FlashChoice) => req<FlashJob>('POST', `/api/flash/${id}/answer`, { choice }),
  flashCancel: (id: number) => req<FlashJob>('POST', `/api/flash/${id}/cancel`),
  flashNow: (id: number) => req<FlashJob>('POST', `/api/flash/${id}/now`),
  /** ota_port: the Arduino OTA endpoint's UDP port (null: switched off). */
  flashSettings: () => req<{ token: string | null; ota_port: number | null }>('GET', '/api/flash/settings'),
  newFlashToken: () => req<{ token: string | null }>('POST', '/api/flash/token'),

  presets: () => req<Preset[]>('GET', '/api/presets'),
  savePreset: (name: string, config: Partial<SessionForm>) => req<Preset>('POST', '/api/presets', { name, config }),
  deletePreset: (id: number) => req<{ deleted: number }>('DELETE', `/api/presets/${id}`),
};
