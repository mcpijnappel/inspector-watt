<script lang="ts">
  // Battery size: saved sizes (stored in the database) plus "add your own".
  import { settings } from '../lib/settings.svelte';

  let adding = $state(false);
  let custom = $state('');

  $effect(() => {
    void settings.load();
  });

  function onchange(e: Event) {
    const v = (e.target as HTMLSelectElement).value;
    if (v === 'add') {
      adding = true;
      (e.target as HTMLSelectElement).value = String(settings.batteries.selected_mah);
    } else settings.selectBattery(Number(v));
  }

  function add(e: SubmitEvent) {
    e.preventDefault();
    const v = parseFloat(custom.replace(',', '.'));
    if (v > 0) settings.addBattery(v);
    adding = false;
    custom = '';
  }
</script>

{#if adding}
  <form class="add" onsubmit={add}>
    <input class="mini" type="text" inputmode="numeric" placeholder="mAh" bind:value={custom} aria-label="Battery capacity in mAh" />
    <button class="btn small" type="submit">Add</button>
    <button class="btn small ghost" type="button" onclick={() => (adding = false)}>Cancel</button>
  </form>
{:else}
  <span class="pick">
    <select class="mini" aria-label="Battery capacity" value={String(settings.batteries.selected_mah)} {onchange}>
      {#each settings.batteries.sizes_mah as s (s)}<option value={String(s)}>{s} mAh</option>{/each}
      <option value="add">Add size…</option>
    </select>
    {#if settings.batteries.sizes_mah.length > 1}
      <button class="x" type="button" title="Remove {settings.batteries.selected_mah} mAh" aria-label="Remove {settings.batteries.selected_mah} mAh from the list"
        onclick={() => settings.removeBattery(settings.batteries.selected_mah)}>×</button>
    {/if}
  </span>
{/if}

<style>
  .mini { background: transparent; border: 1px solid var(--line); border-radius: 6px; color: var(--muted); font-size: 12px; padding: 1px 4px; }
  input.mini { width: 70px; }
  .add, .pick { display: inline-flex; gap: 4px; align-items: center; }
  .x { background: none; border: 0; color: var(--faint); cursor: pointer; font-size: 14px; padding: 0 4px; }
</style>
