<script lang="ts">
  // The head of a Settings section: its title, one line of what it's for, and optionally its main action on the right.
  import type { Snippet } from 'svelte';

  let { title, intro, action }: { title: string; intro: string; action?: Snippet } = $props();
</script>

<div class="s-head">
  <div class="txt">
    <h2>{title}</h2>
    <p>{intro}</p>
  </div>
  {#if action}{@render action()}{/if}
</div>

<style>
  .s-head { display: flex; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
  .txt { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
  h2 { margin: 0; font-size: 18px; font-weight: 600; }
  p { margin: 0; font-size: 13px; color: var(--muted); max-width: 75ch; }
</style>
