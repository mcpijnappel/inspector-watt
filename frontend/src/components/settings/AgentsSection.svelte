<script lang="ts">
  // Agents (MCP): is the server up and who is allowed now; how to connect (this machine / LAN, named tokens);
  // hardware control; what agents did to the hardware; and the tools, folded.
  import { agents, clientName } from '../../lib/agents.svelte';
  import { api, type AgentAction, type McpStatus } from '../../lib/api';
  import { bus } from '../../lib/bus.svelte';
  import { fmtClock } from '../../lib/format';
  import { copyText } from '../../lib/copy';
  import SectionHead from './SectionHead.svelte';

  let st = $state<McpStatus | null>(null);
  let error = $state('');
  let busy = $state(false);
  let shown = $state<number | null>(null); // the token whose value is visible
  /** A token being renewed or removed: asked before. */
  let confirm = $state<{ id: number; what: 'renew' | 'delete' } | null>(null);
  let newName = $state('');
  let copied = $state('');
  let allActions = $state(false);

  async function run(fn: () => Promise<McpStatus>) {
    busy = true;
    error = '';
    try {
      st = await fn();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }
  $effect(() => void run(api.mcp));

  // What agents did to the hardware: refreshed while this section is open.
  let actions = $state<AgentAction[]>([]);
  async function loadActions() {
    try {
      actions = await api.agentActions(30);
    } catch { /* the banner above shows api errors */ }
  }
  $effect(() => {
    void loadActions();
    const t = setInterval(loadActions, 5000);
    return () => clearInterval(t);
  });
  const grants = $derived(agents.state?.grants ?? []);
  const hours = $derived(Math.round((agents.state?.max_grant_s ?? 28800) / 3600));
  const argText = (a: Record<string, unknown>) =>
    Object.entries(a).filter(([, v]) => v != null && v !== '').map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`).join(', ');

  async function setLibrary(on: boolean) {
    await run(() => api.setMcpLibrary(on));
  }

  async function setHardware(on: boolean) {
    await run(() => api.setMcpHardware(on));
    await agents.poll();
  }

  const local = 'claude mcp add --transport http inspector-watt http://localhost:8300/mcp';
  const lanUrl = $derived(`${location.protocol}//${location.host}/mcp`); // https behind a reverse proxy
  const lanCmd = (token: string) => `claude mcp add --transport http inspector-watt ${lanUrl} --header "Authorization: Bearer ${token}"`;
  const masked = (t: string) => `${t.slice(0, 4)}${'•'.repeat(16)}`;

  async function addToken(e: SubmitEvent) {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    await run(() => api.addMcpToken(name));
    if (!error) {
      newName = '';
      shown = st?.tokens.find((t) => t.name === name)?.id ?? null;
    }
  }

  async function copy(text: string, what: string) {
    copied = (await copyText(text)) ? what : '';
    if (copied) setTimeout(() => (copied = copied === what ? '' : copied), 2000);
  }
</script>

<div class="s-section">
  <SectionHead title="Agents (MCP)" intro="Let AI agents such as Claude Code analyse sessions and, if you allow it, run the hardware. They can never flash firmware." />
  {#if error}<div class="s-banner">{error}</div>{/if}

  {#if st}
    <section class="panel s-card">
      <div class="s-status">
        <span class="dot" class:ok={st.server.up} class:bad={!st.server.up}></span>
        <strong class="state">{st.server.up ? 'MCP server running' : 'MCP server not reachable'}</strong>
        {#if st.server.up}
          <span class="muted small">{st.server.tools?.length ?? 0} tools · hardware control {st.hardware ? 'on' : 'off'}{st.hardware ? ` · ${grants.length} session${grants.length === 1 ? '' : 's'} allowed` : ''}</span>
        {:else}<span class="muted small">{st.server.error}</span>{/if}
      </div>
      {#if grants.length}
        <div class="block">
          <div class="s-label">Allowed now</div>
          {#each grants as g (g.session)}
            <div class="grant">
              <span><strong class="state">{clientName(g.client)}</strong> <span class="muted small">{g.via === 'lan' ? `on ${g.host}` : 'on this machine'} · since {fmtClock(g.since).slice(0, 5)} · {g.actions} {g.actions === 1 ? 'action' : 'actions'}</span></span>
              <button class="btn small" type="button" onclick={() => agents.revoke(g.session)}>Revoke</button>
            </div>
          {/each}
        </div>
      {/if}
    </section>

    <h3 class="s-sub">Connect</h3>
    <section class="panel s-card">
      <div class="s-label">On this machine</div>
      <div class="s-cmd"><code>{local}</code><button class="btn small" type="button" onclick={() => copy(local, 'local')}>{copied === 'local' ? 'Copied' : 'Copy'}</button></div>
      <div class="s-row lanrow">
        <div class="txt"><strong class="state">Other machines on the network</strong><span class="hint">Through this web address, one token per machine. Off: only this machine.</span></div>
        <input type="checkbox" checked={st.lan} disabled={busy} aria-label="Other machines on the network"
          onchange={(e) => run(() => api.setMcpLan((e.target as HTMLInputElement).checked))} />
      </div>
      {#if st.lan}
        <div class="tokens">
          {#each st.tokens as t (t.id)}
            {@const value = shown === t.id ? t.token : masked(t.token)}
            <div class="tok">
              <div class="tokhead">
                <span class="tokname"><strong class="state">{t.name}</strong><span class="faint small">{t.created ? `made ${t.created.slice(0, 10)}` : 'from before named tokens'}</span></span>
                <code class="s-token">{value}</code>
                <span class="spacer"></span>
                <button class="btn small ghost" type="button" onclick={() => (shown = shown === t.id ? null : t.id)}>{shown === t.id ? 'Hide' : 'Show'}</button>
                <button class="btn small" type="button" onclick={() => copy(lanCmd(t.token), `cmd${t.id}`)}>{copied === `cmd${t.id}` ? 'Copied' : 'Copy command'}</button>
                <button class="btn small ghost" type="button" onclick={() => (confirm = { id: t.id, what: 'renew' })}>Renew…</button>
                <button class="btn small ghost" type="button" onclick={() => (confirm = { id: t.id, what: 'delete' })}>Remove…</button>
              </div>
              {#if shown === t.id}<div class="s-cmd"><code>{lanCmd(t.token)}</code></div>{/if}
              {#if confirm?.id === t.id}
                <div class="s-confirm">
                  {confirm.what === 'renew' ? `"${t.name}" gets a new value; the machines using it need the new command.` : `Agents using "${t.name}" can't connect any more.`}
                  <button class="btn small" type="button" disabled={busy}
                    onclick={() => { const c = confirm; confirm = null; if (c?.what === 'renew') { shown = t.id; void run(() => api.renewMcpToken(t.id)); } else void run(() => api.deleteMcpToken(t.id)); }}>
                    {confirm.what === 'renew' ? 'Renew' : 'Remove'}</button>
                  <button class="btn small ghost" type="button" onclick={() => (confirm = null)}>Cancel</button>
                </div>
              {/if}
            </div>
          {:else}
            <p class="muted small">No tokens: add one for each machine or agent that connects over the network.</p>
          {/each}
        </div>
        <form class="addtok" onsubmit={addToken}>
          <input class="input" type="text" placeholder="Name, e.g. laptop" bind:value={newName} maxlength="40" aria-label="Name of the new token" />
          <button class="btn small" type="submit" disabled={busy || !newName.trim()}>Add token</button>
        </form>
        <p class="faint small">The name shows in the allow banner and the action log. Anyone who can open this web UI can see the tokens.</p>
      {/if}
    </section>

    {#if st.server.tools?.length}
      <h3 class="s-sub">Hardware control</h3>
      <section class="panel s-card">
        <div class="s-row single">
          <div class="txt"><strong class="state">Let agents control the hardware</strong>
            <span class="hint">Board power and voltage, measuring and recording, the USB console. Each agent session asks you once.</span></div>
          <input type="checkbox" checked={st.hardware} disabled={busy} aria-label="Let agents control the hardware"
            onchange={(e) => setHardware((e.target as HTMLInputElement).checked)} />
        </div>
        <details class="s-fold">
          <summary>How allowing works</summary>
          <div class="inner">
            <ul class="rules small muted">
              <li>Each agent session asks once: a banner appears and you allow or deny it. The allow ends with the session (a new Claude Code session asks again), after {hours} h at the latest, or when you revoke it.</li>
              <li>Claude Code also asks you before <strong>every</strong> hardware action, in every permission mode (auto mode too), and never offers "don't ask again". Other MCP clients don't have that prompt: there, the session allow is the only check.</li>
              <li>Agents on other machines can use it too when network access is on. Switching this off ends every allowed session at once.</li>
              <li>The limits still hold: the device's maximum voltage, and the voltage is always set before power-on.</li>
              <li>This guards the MCP tools. The web UI and its API are open to anyone on your network, so an agent with shell access could still call them directly.</li>
            </ul>
          </div>
        </details>
      </section>

      {#if bus.analyzer}<!-- the sensor library is there only with a bus-decoding module (Settings watches the list) -->
      <h3 class="s-sub">Sensor library</h3>
      <section class="panel s-card">
        <div class="s-row single">
          <div class="txt"><strong class="state">Agents may edit the sensor library</strong>
            <span class="hint">Add or change the tables of chips and sensors (e.g. from a datasheet) and put them on a board. What they save is marked "not checked" until you check it in Settings → Devices & sensors → Sensor library. They can't check or delete tables.</span></div>
          <input type="checkbox" checked={st.library} disabled={busy} aria-label="Agents may edit the sensor library"
            onchange={(e) => setLibrary((e.target as HTMLInputElement).checked)} />
        </div>
      </section>
      {/if}

      <h3 class="s-sub">Recent agent actions</h3>
      <section class="panel">
        {#if actions.length}
          <div class="s-tablewrap">
            <table class="s-table">
              <thead><tr><th>Time</th><th>Agent</th><th>Action</th><th>Result</th></tr></thead>
              <tbody>
                {#each allActions ? actions : actions.slice(0, 10) as a (a.id)}
                  <tr>
                    <td class="mono">{fmtClock(Date.parse(a.at) / 1000)}</td>
                    <td class="who">{clientName(a.client ?? '')}{#if a.host}<span class="sub">{a.host}</span>{/if}</td>
                    <td class="mono act">{a.tool}({argText(a.args)})</td>
                    <td><span class="res" class:ok={a.result === 'ok'} class:bad={a.result !== 'ok'}>{a.result === 'ok' ? 'done' : a.result}</span>{#if a.detail && a.detail !== a.result}<span class="muted small detail"> · {a.detail}</span>{/if}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          {#if actions.length > 10}
            <div class="s-foot"><span>{allActions ? `Newest ${actions.length}` : 'Newest 10'}</span>
              <button class="linkbtn" type="button" onclick={() => (allActions = !allActions)}>{allActions ? 'Show 10' : `Show all ${actions.length}`}</button></div>
          {/if}
        {:else}
          <p class="s-empty">No hardware actions by agents yet.</p>
        {/if}
      </section>

      <details class="panel s-fold">
        <summary>What agents can do ({st.server.tools.length} tools)</summary>
        <div class="inner tools">
          {#each st.server.tools as t (t.name)}
            <div class="tool"><code>{t.name}</code><span class="muted small">{t.description}</span></div>
          {/each}
        </div>
      </details>
    {/if}
  {/if}
</div>

<style>
  .state { font-weight: 500; }
  .block { display: flex; flex-direction: column; gap: 8px; padding-top: 12px; border-top: 1px solid var(--line); }
  .grant { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 6px 10px; }
  .s-cmd code { white-space: nowrap; }
  .lanrow { border-top: 1px solid var(--line); padding-bottom: 0; }
  .single { padding: 0; }
  .tokens { display: flex; flex-direction: column; gap: 8px; }
  .tok { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; }
  .tokhead { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; min-width: 0; }
  .tokname { display: flex; flex-direction: column; gap: 1px; margin-right: 6px; }
  .addtok { display: flex; gap: 8px; max-width: 420px; }
  .addtok .input { min-height: 32px; }
  .rules { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; line-height: 1.5; max-width: 90ch; }
  td.who { white-space: nowrap; }
  td.act { white-space: normal; overflow-wrap: anywhere; min-width: 200px; }
  .res.ok { color: var(--ok); }
  .res.bad { color: var(--rec); }
  .detail { overflow-wrap: anywhere; }
  .tools { gap: 0; }
  .tool { display: grid; grid-template-columns: 170px minmax(0, 1fr); gap: 10px; padding: 8px 0; align-items: baseline; }
  .tool + .tool { border-top: 1px solid var(--line); }
  .tool code { font-family: var(--mono); font-size: 12px; }
  @media (max-width: 560px) { .tool { grid-template-columns: minmax(0, 1fr); gap: 2px; } }
</style>
