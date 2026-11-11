<script lang="ts">
  // Storage overview: disk space, the database, raw data per session (largest first), and the free space below
  // which raw writing stops (a setting).
  import { api, type Storage, type StorageRow } from '../../lib/api';
  import { fmtBytes, fmtDuration } from '../../lib/format';
  import SectionHead from './SectionHead.svelte';

  let st = $state<Storage | null>(null);
  let error = $state('');
  let all = $state(false);
  let minGb = $state('');
  let saved = $state(false);
  async function load() {
    try {
      st = await api.storage();
      minGb = String(st.min_free_gb ?? '');
    } catch (e) {
      error = (e as Error).message;
    }
  }
  $effect(() => void load());
  const minChanged = $derived(st != null && parseFloat(minGb.replace(',', '.')) !== st.min_free_gb);
  async function saveMin(e: SubmitEvent) {
    e.preventDefault();
    const v = parseFloat(minGb.replace(',', '.'));
    if (!(v >= 1 && v <= 10000)) return void (error = 'Keep free: 1 to 10000 GB.');
    error = '';
    try {
      await api.setMeasurement({ min_free_gb: v });
      await load();
      saved = true;
    } catch (err) {
      error = (err as Error).message;
    }
  }

  const pct = (a: number, b: number) => (b > 0 ? (a / b) * 100 : 0);
  function kept(r: StorageRow): string {
    if (!r.raw_bytes) return 'summaries only';
    if (r.raw_kinds.includes('detail')) return 'every sample';
    return 'around wakes';
  }
</script>

<div class="s-section">
  <SectionHead title="Storage" intro="Disk space, and how much each session keeps." />
  {#if error}<div class="s-banner">{error}</div>{/if}
  {#if st}
    {@const d = st.disk}
    {@const minFree = (st.min_free_gb ?? 0) * 2 ** 30}
    <section class="panel card">
      <div class="nums">
        <div><div class="label">Free</div><div class="big mono">{fmtBytes(d.free_bytes)}</div><div class="sub">of {fmtBytes(d.total_bytes)}</div></div>
        <div><div class="label">Raw data</div><div class="big mono">{fmtBytes(st.raw_bytes)}</div><div class="sub">Parquet files</div></div>
        <div><div class="label">Database</div><div class="big mono">{st.database_bytes != null ? fmtBytes(st.database_bytes) : '–'}</div><div class="sub">summaries, logs, events</div></div>
      </div>
      <div class="bar" role="img" aria-label="Disk: {fmtBytes(d.used_bytes)} used of {fmtBytes(d.total_bytes)}">
        <span class="used" style:width="{pct(d.used_bytes - st.raw_bytes, d.total_bytes)}%"></span>
        <span class="raw" style:width="{pct(st.raw_bytes, d.total_bytes)}%"></span>
        {#if minFree}<i class="limit" style:left="{100 - pct(minFree, d.total_bytes)}%" title="Raw writing stops below {st.min_free_gb} GB free"></i>{/if}
      </div>
      <div class="legend small">
        <span><i class="sw used"></i>other files</span><span><i class="sw raw"></i>raw data</span>
        {#if minFree}<span><i class="sw limit"></i>raw writing stops below {st.min_free_gb} GB free (Keep free, below)</span>{/if}
      </div>
      <p class="faint small">Detail keeps every sample (about 0.7 GB per busy hour); soak keeps 10 ms summaries plus raw data around wakes.</p>
      <form class="keep" onsubmit={saveMin}>
        <div class="txt"><strong>Keep free</strong>
          <span class="muted small">Recordings stop writing raw data below this much free disk (their summaries and logs go on).{saved ? ' Saved.' : ''}</span></div>
        <span class="input-unit"><input type="text" inputmode="decimal" bind:value={minGb} oninput={() => (saved = false)} aria-label="Keep this many GB free" size="5" /><span class="muted">GB</span></span>
        <button class="btn small" type="submit" disabled={!minChanged}>Save</button>
      </form>
    </section>

    <section class="panel">
      <div class="wrap">
        <table>
          <thead><tr><th>Session</th><th>Kind</th><th class="num">Length</th><th>Raw kept</th><th class="num">Raw data</th></tr></thead>
          <tbody>
            {#each all ? st.sessions : st.sessions.slice(0, 10) as r (r.id)}
              <tr>
                <td class="name"><a href="#/sessions/{r.id}">#{r.id} {r.name}</a>{#if r.status === 'running'} <span class="rec small">recording</span>{/if}</td>
                <td>{r.kind}</td>
                <td class="num mono">{fmtDuration(r.duration_s)}</td>
                <td class="muted">{kept(r)}{r.raw_bytes && !r.raw_kinds.includes('detail') ? ` (${fmtDuration(r.raw_seconds)})` : ''}</td>
                <td class="num mono">{r.raw_bytes ? fmtBytes(r.raw_bytes) : '–'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if st.sessions.length > 10}
        <div class="s-foot"><span>{all ? `All ${st.sessions.length}, largest first` : `10 largest of ${st.sessions.length}`}</span>
          <button class="linkbtn" type="button" onclick={() => (all = !all)}>{all ? 'Show 10' : `Show all ${st.sessions.length}`}</button></div>
      {/if}
    </section>
  {:else if !error}<p class="faint">Loading…</p>{/if}
</div>

<style>
  .card { padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
  .card p { margin: 0; }
  .keep { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding-top: 10px; border-top: 1px solid var(--line); }
  .keep .txt { flex: 1; min-width: min(240px, 100%); display: flex; flex-direction: column; gap: 3px; }
  .nums { display: flex; flex-wrap: wrap; gap: 14px 36px; }
  .label { font-size: 12px; color: var(--muted); }
  .big { font-size: 22px; font-weight: 700; }
  .sub { font-size: 12px; color: var(--faint); }
  .bar { position: relative; display: flex; height: 12px; border-radius: 6px; background: var(--panel2); overflow: hidden; }
  .bar .used { background: var(--faint); opacity: 0.45; }
  .bar .raw { background: var(--trace); }
  .bar .limit { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--rec); }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 16px; color: var(--muted); }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; }
  .sw.used { background: var(--faint); opacity: 0.45; }
  .sw.raw { background: var(--trace); }
  .sw.limit { width: 3px; background: var(--rec); }
  .wrap { overflow-x: auto; }
  table { width: 100%; min-width: 540px; border-collapse: collapse; }
  th { text-align: left; font-size: 12px; font-weight: 600; color: var(--muted); padding: 8px; }
  td { padding: 7px 8px; border-top: 1px solid var(--line); font-size: 13px; }
  th:first-child, td:first-child { padding-left: 16px; }
  th:last-child, td:last-child { padding-right: 16px; }
  .num { text-align: right; white-space: nowrap; }
  .name a { color: var(--text); text-decoration: none; font-weight: 600; }
  .name a:hover { text-decoration: underline; }
  .rec { color: var(--rec); font-weight: 600; }
</style>
