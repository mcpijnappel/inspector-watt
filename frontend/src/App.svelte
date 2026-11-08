<script lang="ts">
  import AgentBanner from './components/AgentBanner.svelte';
  import FlashBanner from './components/FlashBanner.svelte';
  import TopBar from './components/TopBar.svelte';
  import Compare from './routes/Compare.svelte';
  import Live from './routes/Live.svelte';
  import Session from './routes/Session.svelte';
  import Sessions from './routes/Sessions.svelte';
  import Settings from './routes/Settings.svelte';
  import { agents } from './lib/agents.svelte';
  import { live } from './lib/live.svelte';
  import { router } from './lib/router.svelte';
  import { theme } from './lib/theme.svelte';

  $effect(() => theme.apply());
  $effect(() => {
    live.start();
    agents.start();
    return () => {
      live.stop();
      agents.stop();
    };
  });
</script>

<TopBar />
<AgentBanner />
<FlashBanner />
{#if router.route.name === 'live'}
  <Live />
{:else if router.route.name === 'session' && router.route.id != null}
  {#key router.route.id}<Session id={router.route.id} />{/key}
{:else if router.route.name === 'sessions'}
  {#key router.route.query.toString()}<Sessions />{/key}
{:else if router.route.name === 'settings'}
  <Settings section={router.route.section} />
{:else}
  <!-- keyed: following a new compare link (e.g. from the session list) starts fresh -->
  {#key router.route.query.toString()}<Compare />{/key}
{/if}
