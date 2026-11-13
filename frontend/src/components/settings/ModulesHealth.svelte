<script lang="ts">
  // One card per module on Settings → Health: installed or not, switched on, and what the module says about itself.
  // The logic analyzer also shows its device and stream.
  import { api, type ModuleInfo } from '../../lib/api';

  type Stream = Awaited<ReturnType<typeof api.analyzerStatus>>;
  let mods = $state<ModuleInfo[] | null>(null);
  let analyzer = $state<Stream | null>(null);

  $effect(() => {
    const load = async () => {
      try {
        mods = await api.modules();
        const a = mods.find((m) => m.name === 'analyzer');
        analyzer = a?.installed && a.values.enabled !== false ? await api.analyzerStatus().catch(() => null) : null;
      } catch { /* the page's banner shows an unreachable api */ }
    };
    void load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  });

  type Row = [string, string, ('ok' | 'warn' | 'bad' | '')?];
  function rows(m: ModuleInfo): Row[] {
    if (!m.installed) return [['State', 'not installed'], ['Turn on', `COMPOSE_PROFILES=${m.name} in .env, then ./dc up -d`]];
    const h = m.module?.health;
    const on = m.values.enabled !== false;
    const out: Row[] = [
      ['State', on ? 'on' : 'switched off', on ? 'ok' : ''],
      ['Health', h?.detail ?? (h?.ok ? 'fine' : '–'), h?.ok ? 'ok' : on ? 'warn' : ''],
    ];
    if (m.name === 'analyzer' && analyzer && on) {
      const d = analyzer.devices[0], s = analyzer.stream;
      out.push(['Analyzer', d ? `${d.description}${d.conn ? ` · USB ${d.conn}` : ''}` : 'none found', d ? '' : 'warn']);
      out.push(['Stream', s ? (s.running ? `${(s.rate / 1e6).toFixed(0)} MHz · ${s.buffered_s} of ${s.buffer_s} s kept` : s.error ?? 'starting') : 'not running', s?.running ? 'ok' : 'warn']);
      if (s) out.push(['Restarts', String(s.restarts), s.restarts ? 'warn' : '']);
      const v = m.values;
      out.push(['Wiring', [v.i2c_sda !== 'off' && v.i2c_scl !== 'off' ? `I2C ${v.i2c_sda}/${v.i2c_scl}` : '', v.spi_clk !== 'off' ? `SPI clock ${v.spi_clk}` : ''].filter(Boolean).join(' · ') || 'nothing wired']);
    }
    if (m.module?.version) out.push(['Version', m.module.version]);
    return out;
  }
</script>

{#if mods?.length}
  <section class="panel mods" aria-label="Modules">
    <div class="head"><h3>Modules</h3><span class="muted small">optional extras, each in its own container</span></div>
    <div class="grid">
      {#each mods as m (m.name)}
        {@const ok = m.installed && m.values.enabled !== false && m.module?.health?.ok}
        <div class="mod" class:absent={!m.installed}>
          <h4><span class="dot" class:ok class:warn={m.installed && m.values.enabled !== false && !ok}></span>{m.title}</h4>
          <dl>
            {#each rows(m) as [k, v, state] (k)}
              <dt>{k}</dt><dd class="mono" class:ok={state === 'ok'} class:warn={state === 'warn'} class:bad={state === 'bad'}>{v}</dd>
            {/each}
          </dl>
          {#if m.installed}<a class="small" href="#/settings/modules">Module settings →</a>{/if}
        </div>
      {/each}
    </div>
  </section>
{/if}

<style>
  .head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; padding: 12px 16px; border-bottom: 1px solid var(--line); }
  h3 { margin: 0; font-size: 14px; }
  .small { font-size: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; padding: 12px 16px; }
  .mod { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .mod.absent { border-style: dashed; }
  .mod.absent h4 { color: var(--muted); }
  h4 { margin: 0; font-size: 14px; display: flex; align-items: center; gap: 8px; }
  dl { display: grid; grid-template-columns: auto minmax(0, 1fr); margin: 0; }
  dt, dd { margin: 0; padding: 5px 0; border-top: 1px solid var(--line); font-size: 12px; }
  dt:first-of-type, dt:first-of-type + dd { border-top: 0; }
  dt { color: var(--muted); padding-right: 12px; }
  dd { text-align: right; overflow-wrap: anywhere; }
  dd.ok { color: var(--ok); }
  dd.warn { color: var(--warn); }
  dd.bad { color: var(--rec); }
  a { text-decoration: none; }
</style>
