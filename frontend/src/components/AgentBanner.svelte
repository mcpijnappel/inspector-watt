<script lang="ts">
  // Under the top bar, on every page: an agent (MCP) asks to control the hardware. Allow it for its session, or deny.
  import { agents, clientName } from '../lib/agents.svelte';
  import { live } from '../lib/live.svelte';

  const pending = $derived(agents.state?.pending ?? []);
  const now = $derived(live.status?.time ?? Date.now() / 1000);
  const hours = $derived(Math.round((agents.state?.max_grant_s ?? 28800) / 3600));
  let busy = $state(false);

  function argText(args: Record<string, unknown>): string {
    return Object.entries(args)
      .filter(([, v]) => v != null && v !== '')
      .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
      .join(', ');
  }

  async function answer(id: number, allow: boolean) {
    busy = true;
    await agents.answer(id, allow);
    busy = false;
  }
</script>

{#each pending as r (r.id)}
  {@const left = Math.max(0, Math.round((r.expires_at ?? now) - now))}
  <div class="banner" role="alertdialog" aria-label="Agent asks for hardware control">
    <svg class="ic" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="4" y="8" width="16" height="12" rx="2"></rect><path d="M12 8V4M8 13h.01M16 13h.01M9 17h6"></path></svg>
    <div class="what">
      <span class="t">{clientName(r.client)} {r.via === 'lan' ? `on ${r.host}` : 'on this machine'} asks to control the hardware</span>
      <span class="s">First: <span class="mono">{r.tool}({argText(r.args)})</span> · {r.client}</span>
    </div>
    <span class="spacer"></span>
    <div class="acts">
      <button class="btn small primary" type="button" disabled={busy} onclick={() => answer(r.id, true)}>Allow for this session</button>
      <button class="btn small ghost" type="button" disabled={busy} onclick={() => answer(r.id, false)}>Deny</button>
    </div>
    <span class="faint small full">
      Lasts until that agent session ends, at most {hours} h; revoke any time from the top bar. Claude Code still asks before every action.
      The agent waits {Math.floor(left / 60)}:{String(left % 60).padStart(2, '0')} more.
    </span>
  </div>
{/each}

<style>
  .banner { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; padding: 10px 24px; border-bottom: 1px solid var(--line); font-size: 13px; background: var(--warn-bg); }
  .ic { flex: 0 0 auto; color: var(--warn); }
  .what { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .t { font-weight: 600; }
  .s { color: var(--muted); overflow-wrap: anywhere; }
  .acts { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .full { flex-basis: 100%; }
  .small { font-size: 12px; }
  @media (max-width: 720px) { .banner { padding: 10px 12px; } }
</style>
