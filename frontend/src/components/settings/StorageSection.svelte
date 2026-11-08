<script lang="ts">
  // Storage overview: disk space, the database, and raw data per session (largest first).
  import { api, type Storage, type StorageRow } from '../../lib/api';
  import { fmtBytes, fmtDuration } from '../../lib/format';

  let st = $state<Storage | null>(null);
  let error = $state('');
  $effect(() => {
    api.storage().then((r) => (st = r)).catch((e) => (error = (e as Error).message));
  });

  const pct = (a: number, b: number) => (b > 0 ? (a / b) * 100 : 0);
  function kept(r: StorageRow): string {
    if (!r.raw_bytes) return 'summaries only';
    if (r.raw_kinds.includes('detail')) return 'every sample';
    return 'around wakes';
  }
</script>

<div class="section">
  <h2 class="title">Storage</h2>
  {#if error}<div class="banner bad">{error}</div>{/if}
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
        {#if minFree}<span><i class="sw limit"></i>raw writing stops below {st.min_free_gb} GB free (MIN_FREE_GB)</span>{/if}
      </div>
      <p class="muted small">A detail session keeps every sample (about 0.7 GB per hour while the board is busy); a soak keeps 10 ms summaries plus raw data around wakes.</p>
    </section>

    <section class="panel">
      <div class="wrap">
        <table>
          <thead><tr><th>Session</th><th>Kind</th><th class="num">Length</th><th>Raw kept</th><th class="num">Raw data</th></tr></thead>
          <tbody>
            {#each st.sessions as r (r.id)}
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
    </section>
  {:else if !error}<p class="faint">Loading…</p>{/if}
</div>

<style>
  .section { display: flex; flex-direction: column; gap: 12px; }
  .title { margin: 0; font-size: 18px; }
  .small { font-size: 12px; }
  .banner { padding: 10px 14px; border-radius: 10px; }
  .banner.bad { border: 1px solid var(--rec); color: var(--rec); background: var(--rec-bg); }
  .card { padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
  .card p { margin: 0; }
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
