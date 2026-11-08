<script lang="ts">
  // Agents (MCP): is the server up, how to connect (this machine / LAN), LAN access and its named tokens.
  import { agents, clientName } from '../../lib/agents.svelte';
  import { api, type AgentAction, type McpStatus } from '../../lib/api';
  import { fmtClock } from '../../lib/format';
  import { copyText } from '../../lib/copy';

  let st = $state<McpStatus | null>(null);
  let error = $state('');
  let busy = $state(false);
  let shown = $state<number | null>(null); // the token whose value is visible
  /** A token being renewed or removed: asked before. */
  let confirm = $state<{ id: number; what: 'renew' | 'delete' } | null>(null);
  let newName = $state('');
  let copied = $state('');

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

<div class="section">
  <h2 class="title">Agents (MCP)</h2>
  <p class="intro muted">AI agents such as Claude Code can use this tracker through MCP: list and analyse sessions, search the serial log, summarise cycles, estimate battery life, look at the live measurement, and edit phase rules and cycle types. Controlling the hardware is off unless you switch it on below, and then needs your OK per agent session and per action.</p>
  {#if error}<div class="banner bad">{error}</div>{/if}

  {#if st}
    <section class="panel card">
      <div class="status">
        <span class="dot" class:ok={st.server.up} class:bad={!st.server.up}></span>
        <strong>{st.server.up ? 'MCP server running' : 'MCP server not reachable'}</strong>
        {#if st.server.up}<span class="muted small">{st.server.tools?.length ?? 0} tools</span>{:else}<span class="muted small">{st.server.error}</span>{/if}
      </div>

      <div class="block">
        <div class="label">On this machine</div>
        <div class="cmd"><code>{local}</code><button class="btn small" type="button" onclick={() => copy(local, 'local')}>{copied === 'local' ? 'Copied' : 'Copy'}</button></div>
      </div>

      <div class="block">
        <label class="lan">
          <input type="checkbox" checked={st.lan} disabled={busy} onchange={(e) => run(() => api.setMcpLan((e.target as HTMLInputElement).checked))} />
          <span><strong>Other machines on the network</strong><span class="muted small">Through this web address, with a token. Off: only this machine.</span></span>
        </label>
        {#if st.lan}
          <div class="tokens">
            {#each st.tokens as t (t.id)}
              {@const value = shown === t.id ? t.token : masked(t.token)}
              <div class="tok">
                <div class="tokhead">
                  <strong>{t.name}</strong>
                  <code class="token">{value}</code>
                  <span class="spacer"></span>
                  <button class="btn small ghost" type="button" onclick={() => (shown = shown === t.id ? null : t.id)}>{shown === t.id ? 'Hide' : 'Show'}</button>
                  <button class="btn small" type="button" onclick={() => copy(lanCmd(t.token), `cmd${t.id}`)}>{copied === `cmd${t.id}` ? 'Copied' : 'Copy command'}</button>
                  <button class="btn small ghost" type="button" onclick={() => (confirm = { id: t.id, what: 'renew' })}>Renew…</button>
                  <button class="btn small ghost" type="button" onclick={() => (confirm = { id: t.id, what: 'delete' })}>Remove…</button>
                </div>
                {#if shown === t.id}<div class="cmd"><code>{lanCmd(t.token)}</code></div>{/if}
                {#if confirm?.id === t.id}
                  <div class="confirm small">
                    {confirm.what === 'renew' ? `"${t.name}" gets a new value; the machines using it need the new command.` : `Agents using "${t.name}" can't connect any more.`}
                    <button class="btn small" type="button" disabled={busy}
                      onclick={() => { const c = confirm; confirm = null; if (c?.what === 'renew') { shown = t.id; void run(() => api.renewMcpToken(t.id)); } else void run(() => api.deleteMcpToken(t.id)); }}>
                      {confirm.what === 'renew' ? 'Renew' : 'Remove'}</button>
                    <button class="btn small ghost" type="button" onclick={() => (confirm = null)}>Cancel</button>
                  </div>
                {/if}
                <span class="faint small">{t.created ? `made ${t.created.slice(0, 10)}` : 'from before named tokens'}</span>
              </div>
            {:else}
              <p class="muted small">No tokens: add one for each machine or agent that connects over the network.</p>
            {/each}
          </div>
          <form class="addtok" onsubmit={addToken}>
            <input class="input" type="text" placeholder="Name, e.g. laptop" bind:value={newName} maxlength="40" aria-label="Name of the new token" />
            <button class="btn small" type="submit" disabled={busy || !newName.trim()}>Add token</button>
          </form>
          <p class="faint small">One token per machine or agent: remove one without touching the others. Its name shows in the hardware-control banner and the action log. Anyone who can open this web UI can see the tokens, like they can use the app itself.</p>
        {/if}
      </div>
    </section>

    {#if st.server.tools?.length}
      <h3 class="sub">Hardware control</h3>
      <section class="panel card">
        <label class="lan">
          <input type="checkbox" checked={st.hardware} disabled={busy} onchange={(e) => setHardware((e.target as HTMLInputElement).checked)} />
          <span><strong>Let agents control the hardware</strong>
            <span class="muted small">DUT power and voltage, measuring and recording, the DUT's USB console. Never flashing firmware (that's PlatformIO with the upload token).</span></span>
        </label>
        <ul class="rules small muted">
          <li>Each agent session asks once: a banner appears here and you allow that session or deny it. The allow ends with the session (a new Claude Code session asks again), after {hours} h at the latest, or when you revoke it.</li>
          <li>Claude Code also asks you before <strong>every</strong> hardware action, in every permission mode (auto mode too), and never offers "don't ask again". Other MCP clients don't have that prompt: there, the session allow is the only check.</li>
          <li>Agents on other machines can use it too when network access above is on. Switching this off ends every allowed session at once.</li>
          <li>The limits still hold: the device's maximum voltage, and the voltage is always set before power-on.</li>
        </ul>
        {#if grants.length}
          <div class="block">
            <div class="label">Allowed now</div>
            {#each grants as g (g.session)}
              <div class="grant">
                <span><strong>{clientName(g.client)}</strong> <span class="muted">{g.via === 'lan' ? `on ${g.host}` : 'on this machine'} · since {fmtClock(g.since).slice(0, 5)} · {g.actions} {g.actions === 1 ? 'action' : 'actions'}</span></span>
                <button class="btn small" type="button" onclick={() => agents.revoke(g.session)}>Revoke</button>
              </div>
            {/each}
          </div>
        {/if}
        <p class="faint small">This guards the MCP tools. The tracker's web UI and its API are open to anyone on your network, so an agent with shell access could still call them directly.</p>
      </section>

      <h3 class="sub">Recent agent actions</h3>
      <section class="panel">
        {#if actions.length}
          <div class="tablewrap">
            <table>
              <thead><tr><th>Time</th><th>Agent</th><th>Action</th><th>Result</th></tr></thead>
              <tbody>
                {#each actions as a (a.id)}
                  <tr>
                    <td class="mono">{fmtClock(Date.parse(a.at) / 1000)}</td>
                    <td>{clientName(a.client ?? '')} <span class="muted">{a.host ?? ''}</span></td>
                    <td class="mono act">{a.tool}({argText(a.args)})</td>
                    <td><span class="res" class:ok={a.result === 'ok'} class:bad={a.result !== 'ok'}>{a.result === 'ok' ? 'done' : a.result}</span>{#if a.detail && a.detail !== a.result}<span class="muted small detail"> · {a.detail}</span>{/if}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {:else}
          <p class="empty muted">No hardware actions by agents yet.</p>
        {/if}
      </section>

      <h3 class="sub">What agents can do</h3>
      <section class="panel tools">
        {#each st.server.tools as t (t.name)}
          <div class="tool"><code>{t.name}</code><span class="muted small">{t.description}</span></div>
        {/each}
      </section>
    {/if}
  {/if}
</div>

<style>
  .tokens { display: flex; flex-direction: column; gap: 8px; }
  .tok { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; }
  .tokhead { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; min-width: 0; }
  .addtok { display: flex; gap: 8px; max-width: 420px; }
  .addtok .input { min-height: 32px; }
  .rules { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; line-height: 1.5; max-width: 90ch; }
  .grant { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 6px 10px; }
  .tablewrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { text-align: left; padding: 8px 12px; border-top: 1px solid var(--line); vertical-align: top; }
  th { font-weight: 500; color: var(--faint); font-size: 12px; border-top: 0; white-space: nowrap; }
  td.mono { font-family: var(--mono); font-size: 12px; white-space: nowrap; }
  td.act { white-space: normal; overflow-wrap: anywhere; min-width: 200px; }
  .res.ok { color: var(--ok); }
  .res.bad { color: var(--rec); }
  .detail { overflow-wrap: anywhere; }
  .empty { padding: 14px 16px; font-size: 13px; }
  .section { display: flex; flex-direction: column; gap: 12px; }
  .title { margin: 0; font-size: 18px; }
  .sub { margin: 8px 0 0; font-size: 15px; }
  .intro { margin: 0; font-size: 13px; max-width: 80ch; }
  .small { font-size: 12px; }
  .banner { padding: 10px 14px; border-radius: 10px; }
  .banner.bad { border: 1px solid var(--rec); color: var(--rec); background: var(--rec-bg); }
  .card { padding: 14px 16px; display: flex; flex-direction: column; gap: 14px; }
  .status { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .block { display: flex; flex-direction: column; gap: 8px; padding-top: 12px; border-top: 1px solid var(--line); }
  .label { font-weight: 600; font-size: 13px; }
  .cmd { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .cmd code { flex: 1; min-width: 0; padding: 8px 10px; border-radius: 8px; background: var(--panel2); font-family: var(--mono); font-size: 12px; overflow-x: auto; white-space: nowrap; }
  .lan { display: flex; align-items: flex-start; gap: 10px; cursor: pointer; }
  .lan input { width: 18px; height: 18px; margin-top: 2px; }
  .lan > span { display: flex; flex-direction: column; gap: 2px; }
  .token { font-family: var(--mono); font-size: 12px; padding: 4px 8px; border-radius: 6px; background: var(--panel2); overflow-wrap: anywhere; }
  .confirm { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px; }
  p { margin: 0; }
  .tools { display: flex; flex-direction: column; }
  .tool { display: grid; grid-template-columns: 170px minmax(0, 1fr); gap: 10px; padding: 8px 16px; align-items: baseline; }
  .tool + .tool { border-top: 1px solid var(--line); }
  .tool code { font-family: var(--mono); font-size: 12px; }
  @media (max-width: 560px) { .tool { grid-template-columns: minmax(0, 1fr); gap: 2px; } }
</style>
