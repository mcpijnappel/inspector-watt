<script lang="ts">
  import Sparkline from '../components/Sparkline.svelte';
  import { api, moduleBadges, type SessionSummary } from '../lib/api';
  import { fmtAmps, fmtBytes, fmtCharge, fmtSpan, fmtStarted } from '../lib/format';
  import { devices } from '../lib/devices.svelte';
  import { live } from '../lib/live.svelte';
  import { router } from '../lib/router.svelte';

  let sessions = $state<SessionSummary[]>([]);
  let loaded = $state(false);
  let error = $state('');
  let query = $state('');
  let kind = $state<'all' | 'detail' | 'soak'>('all');
  /** Device filter: from the Devices page link (?device=), or picked here. */
  let device = $state<number | 'all'>(Number(router.route.query.get('device')) || 'all');
  $effect(() => devices.ensure());
  let picked = $state<number[]>([]);
  let confirmDelete = $state<number | null>(null);

  async function load() {
    try {
      sessions = await api.sessions();
      error = '';
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loaded = true;
    }
  }

  $effect(() => {
    void load();
    const t = setInterval(() => void load(), 5000); // running sessions grow
    return () => clearInterval(t);
  });

  const shown = $derived(
    sessions.filter((s) => (kind === 'all' || s.kind === kind) && (device === 'all' || s.device_id === device) && (!query.trim() || `${s.name} ${s.notes}`.toLowerCase().includes(query.trim().toLowerCase()))),
  );
  const totalRaw = $derived(sessions.reduce((a, s) => a + (s.raw_bytes ?? 0), 0));
  const recordingId = $derived(live.status?.session?.active ? live.status.session.session?.id : undefined);

  function toggle(id: number) {
    picked = picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id].slice(-2);
  }

  async function remove(id: number) {
    try {
      await api.deleteSession(id);
      picked = picked.filter((x) => x !== id);
      confirmDelete = null;
      await load();
    } catch (e) {
      error = (e as Error).message;
    }
  }
</script>

<div class="page">
  <div class="head">
    <h1>Sessions</h1>
    <label class="search">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4-4"></path></svg>
      <span class="sr-only">Search sessions</span>
      <input type="search" placeholder="Search names and notes" bind:value={query} />
    </label>
    <div class="seg" role="group" aria-label="Kind">
      <button type="button" aria-pressed={kind === 'all'} onclick={() => (kind = 'all')}>All</button>
      <button type="button" aria-pressed={kind === 'detail'} onclick={() => (kind = 'detail')}>Detail</button>
      <button type="button" aria-pressed={kind === 'soak'} onclick={() => (kind = 'soak')}>Soak</button>
    </div>
    {#if devices.list.length > 1 || device !== 'all'}
      <select class="devsel" bind:value={device} aria-label="Device">
        <option value="all">All devices</option>
        {#each devices.list as d (d.id)}<option value={d.id}>{d.name}</option>{/each}
      </select>
    {/if}
    <span class="spacer"></span>
    <a class="btn" class:primary={picked.length === 2} href={picked.length === 2 ? `#/compare?a=${picked[0]}&b=${picked[1]}` : undefined}
      aria-disabled={picked.length !== 2}>Compare selected ({picked.length}/2)</a>
  </div>

  {#if error}<div class="banner bad">{error}</div>{/if}

  <div class="panel tablewrap">
    <table>
      <thead>
        <tr>
          <th scope="col" class="sel"><span class="sr-only">Select</span></th>
          <th scope="col">Session</th>
          <th scope="col">Kind</th>
          <th scope="col">Started</th>
          <th scope="col" class="num">Duration</th>
          <th scope="col" class="num">Avg current</th>
          <th scope="col" class="num">Charge</th>
          <th scope="col" class="num">Wakes</th>
          <th scope="col" class="num">Raw data</th>
          <th scope="col" class="spark">Activity</th>
          <th scope="col"><span class="sr-only">Actions</span></th>
        </tr>
      </thead>
      <tbody>
        {#each shown as s (s.id)}
          <tr>
            <td class="sel"><input type="checkbox" checked={picked.includes(s.id)} onchange={() => toggle(s.id)} aria-label="Select {s.name} for comparing" /></td>
            <td>
              <a class="name" href="#/sessions/{s.id}">{s.name}</a>
              {#if s.status === 'running'}<span class="badge rec">{s.id === recordingId ? 'recording' : 'running'}</span>{/if}
              {#if s.status === 'interrupted'}<span class="badge warn">interrupted</span>{/if}
              {#each moduleBadges(s.modules) as b (b.label)}<span class="badge mod" title={b.title}>{b.label}</span>{/each}
              {#if s.notes || (s.device_name && devices.list.length > 1)}<div class="notes">{[devices.list.length > 1 ? s.device_name : null, s.notes].filter(Boolean).join(' · ')}</div>{/if}
            </td>
            <td><span class="badge {s.kind}">{s.kind}</span></td>
            <td class="mono muted">{fmtStarted(s.started_at)}</td>
            <td class="num mono">{fmtSpan(s.duration_s)}</td>
            <td class="num mono strong">{fmtAmps(s.avg_current_a)}</td>
            <td class="num mono">{fmtCharge(s.charge_c)}</td>
            <td class="num mono">{s.wakes ?? 0}</td>
            <td class="num mono muted">{s.raw_bytes ? fmtBytes(s.raw_bytes) : '–'}</td>
            <td class="spark"><Sparkline id={s.id} duration={s.duration_s} label="Peak current over {s.name}" /></td>
            <td class="act">
              {#if confirmDelete === s.id}
                <span class="confirm">Delete for good?
                  <button class="btn small danger" type="button" onclick={() => remove(s.id)}>Delete</button>
                  <button class="btn small ghost" type="button" onclick={() => (confirmDelete = null)}>Cancel</button>
                </span>
              {:else}
                <button class="btn small ghost icon" type="button" disabled={s.status === 'running'} title={s.status === 'running' ? 'Stop it first' : 'Delete'}
                  aria-label="Delete {s.name}" onclick={() => (confirmDelete = s.id)}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"></path></svg>
                </button>
              {/if}
            </td>
          </tr>
        {:else}
          <tr><td colspan="11" class="empty">{loaded ? (sessions.length ? 'No sessions match.' : 'No sessions yet. Press Record, or Save… live data.') : 'Loading…'}</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
  <div class="foot muted">{sessions.length} sessions · {fmtBytes(totalRaw)} raw data · gaps (restarts, replugs) show as breaks in the charts</div>
</div>

<style>
  .page { max-width: 1680px; margin: 0 auto; padding: 20px 24px 28px; display: flex; flex-direction: column; gap: 16px; }
  .head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
  h1 { margin: 0 8px 0 0; font-size: 22px; }
  .search { display: flex; align-items: center; gap: 6px; padding: 0 10px; min-height: 38px; border: 1px solid var(--line); border-radius: 8px; color: var(--muted); background: var(--panel); }
  .search input { width: 220px; border: 0; background: transparent; outline: none; }
  a[aria-disabled='true'] { opacity: 0.55; pointer-events: none; }
  .banner.bad { padding: 10px 14px; border-radius: 10px; border: 1px solid var(--rec); color: var(--rec); background: var(--rec-bg); }
  .tablewrap { overflow-x: auto; }
  table { width: 100%; min-width: 1100px; border-collapse: collapse; }
  th { text-align: left; font-size: 12px; font-weight: 600; color: var(--muted); padding: 12px 8px; }
  td { padding: 10px 8px; border-top: 1px solid var(--line); vertical-align: middle; font-size: 13px; }
  th:first-child, td:first-child { padding-left: 16px; }
  .num { text-align: right; }
  .strong { font-weight: 600; }
  .sel { width: 32px; }
  .sel input { width: 18px; height: 18px; accent-color: var(--accent); }
  .spark { width: 220px; }
  .name { font-weight: 600; color: var(--text); text-decoration: none; font-size: 14px; }
  .name:hover { text-decoration: underline; }
  .notes { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .devsel { min-height: 34px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel); color: var(--text); padding: 0 8px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; margin-left: 6px; }
  td > .badge { margin-left: 0; }
  .badge.detail { color: var(--accent); background: var(--sel); }
  .badge.soak { color: #a371f7; background: rgba(163, 113, 247, 0.14); }
  .badge.rec { color: var(--rec); background: var(--rec-bg); }
  .badge.warn { color: var(--warn); background: rgba(227, 179, 65, 0.14); }
  .badge.mod { color: var(--flash); background: var(--flash-bg); font-weight: 500; }
  .act { width: 1%; white-space: nowrap; text-align: right; padding-right: 16px; }
  .confirm { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
  :global(.btn.danger) { background: var(--rec-strong); border-color: var(--rec-strong); color: #fff; }
  .empty { text-align: center; color: var(--faint); padding: 28px; }
  .foot { font-size: 12px; }
</style>
