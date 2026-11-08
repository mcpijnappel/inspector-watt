<script lang="ts">
  // Top-bar chip while one or more agent sessions may control the hardware: who, until when, what they did; revoke.
  import { agents, clientName } from '../lib/agents.svelte';
  import { fmtClock } from '../lib/format';
  import { outside } from '../lib/outside';
  import { place } from '../lib/place';

  let open = $state(false);
  const grants = $derived(agents.state?.grants ?? []);
</script>

{#if grants.length}
  <div class="anchor">
    <button class="chip warn" type="button" aria-expanded={open} onclick={() => (open = !open)} title="Agents (MCP) that may control the hardware">
      <span class="dot warn"></span>Agent control{grants.length > 1 ? ` · ${grants.length}` : ''}
    </button>
    {#if open}
      <div class="popover menu" role="dialog" aria-label="Agent control" use:place use:outside={() => (open = false)}>
        <div class="hd">Agents with hardware control</div>
        {#each grants as g (g.session)}
          <div class="grant">
            <div class="who"><strong>{clientName(g.client)}</strong> <span class="muted">{g.via === 'lan' ? `on ${g.host}` : 'on this machine'}</span></div>
            <div class="small muted">since {fmtClock(g.since).slice(0, 5)} · until {fmtClock(g.until).slice(0, 5)} at the latest · {g.actions} {g.actions === 1 ? 'action' : 'actions'}{g.last_action ? `, last ${g.last_action}` : ''}</div>
            <div><button class="btn small" type="button" onclick={() => agents.revoke(g.session)}>Revoke</button></div>
          </div>
        {/each}
        <p class="small faint">Claude Code still asks before every action. A new agent session asks here again.</p>
        <a href="#/settings/agents" onclick={() => (open = false)}>Agent settings and log →</a>
      </div>
    {/if}
  </div>
{/if}

<style>
  .anchor { position: relative; }
  .chip.warn { border-color: var(--warn); color: var(--warn); }
  .menu { right: 0; top: calc(100% + 6px); width: 340px; padding: 14px; display: flex; flex-direction: column; gap: 10px; font-size: 13px; }
  .hd { font-weight: 600; }
  .grant { display: flex; flex-direction: column; gap: 6px; padding-top: 10px; border-top: 1px solid var(--line); }
  .small { font-size: 12px; }
  p { margin: 0; }
  a { text-decoration: none; }
</style>
