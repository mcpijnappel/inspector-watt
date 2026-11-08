<script lang="ts">
  // Keep live data that wasn't recorded: the last few minutes, or the selected range.
  import { api } from '../lib/api';
  import { autoName } from '../lib/format';
  import { live, PERIOD } from '../lib/live.svelte';
  import { outside } from '../lib/outside';
  import { place } from '../lib/place';

  let { onclose }: { onclose: () => void } = $props();

  type Choice = { id: string; label: string; a: number; b: number };
  const endS = $derived((live.status?.buffer.end_idx ?? 0) * PERIOD);
  const startS = $derived(live.bufferStart);
  const choices = $derived.by<Choice[]>(() => {
    const out: Choice[] = [];
    if (live.selection) out.push({ id: 'sel', label: `Selection (${(live.selection.b - live.selection.a).toFixed(3)} s)`, ...live.selection });
    if (live.paused && live.view) out.push({ id: 'view', label: `What's on screen (${(live.view.b - live.view.a).toFixed(1)} s)`, ...live.view });
    for (const m of [1, 2, 5]) {
      const a = Math.max(startS, endS - m * 60);
      out.push({ id: `last${m}`, label: `Last ${m} min${endS - a < m * 60 - 1 ? ` (only ${Math.round(endS - a)} s in the buffer)` : ''}`, a, b: endS });
    }
    return out;
  });

  let choice = $state(live.selection ? 'sel' : live.paused ? 'view' : 'last2');
  let name = $state(autoName('live'));
  let busy = $state(false);
  let error = $state('');
  let saved = $state<{ id: number; s: number } | null>(null);
  const recording = $derived(!!live.status?.session?.active);

  async function save(e: SubmitEvent) {
    e.preventDefault();
    const c = choices.find((x) => x.id === choice);
    if (!c) return;
    busy = true;
    error = '';
    try {
      const r = await api.saveBuffer(name.trim() || autoName('live'), Math.round(c.a / PERIOD), Math.round(c.b / PERIOD));
      saved = { id: r.saved, s: r.duration_s };
    } catch (err) {
      error = (err as Error).message;
    } finally {
      busy = false;
    }
  }
</script>

<div class="popover menu" use:outside={onclose} use:place role="dialog" aria-label="Save live data">
  {#if recording}
    <p class="muted">A session is recording, so this data is already being stored.</p>
  {:else if saved}
    <p>Saved {saved.s.toFixed(1)} s as session #{saved.id}.</p>
    <div class="row"><a class="btn" href="#/sessions/{saved.id}" onclick={onclose}>Open</a><button class="btn ghost" type="button" onclick={onclose}>Close</button></div>
  {:else}
    <form onsubmit={save}>
      <div class="title">Save live data as a session</div>
      <fieldset>
        <legend class="sr-only">What to save</legend>
        {#each choices as c (c.id)}
          <label class="check"><input type="radio" name="what" value={c.id} bind:group={choice} />{c.label}</label>
        {/each}
      </fieldset>
      <label class="field">Name<input class="input" type="text" bind:value={name} /></label>
      <div class="hint">Full 10 µs resolution plus the serial log. The live buffer keeps the last 5 minutes.</div>
      {#if error}<div class="error">{error}</div>{/if}
      <div class="row"><button class="btn ghost" type="button" onclick={onclose}>Cancel</button><button class="btn primary" type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button></div>
    </form>
  {/if}
</div>

<style>
  .menu { right: 0; top: calc(100% + 8px); width: 340px; padding: 14px; }
  form { display: flex; flex-direction: column; gap: 12px; }
  .title { font-weight: 600; }
  fieldset { border: 0; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .row { display: flex; gap: 8px; justify-content: flex-end; }
  p { margin: 0 0 12px; }
</style>
