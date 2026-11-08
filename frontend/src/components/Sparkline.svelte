<script lang="ts">
  // Small peak-current overview of a whole session (log scale), loaded when it scrolls into view.
  import { api } from '../lib/api';

  let { id, duration, label }: { id: number; duration: number; label: string } = $props();

  let path = $state('');
  let el: SVGSVGElement;
  const W = 1000;
  const H = 60;
  const LO = Math.log10(1e-6);
  const HI = Math.log10(1);

  $effect(() => {
    if (!(duration > 0)) return;
    const io = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      io.disconnect();
      api.trace(id, 0, duration, 200).then((t) => {
        const y = (v: number) => H - ((Math.log10(Math.max(v, 1e-6)) - LO) / (HI - LO)) * H;
        let d = '';
        let open = false;
        t.x.forEach((x, i) => {
          const v = t.max[i];
          const px = (x / duration) * W;
          if (v == null) {
            if (open) d += ` L${px.toFixed(1)},${H} Z`;
            open = false;
            return;
          }
          d += open ? ` L${px.toFixed(1)},${y(v).toFixed(1)}` : ` M${px.toFixed(1)},${H} L${px.toFixed(1)},${y(v).toFixed(1)}`;
          open = true;
        });
        if (open) d += ` L${W},${H} Z`;
        path = d;
      }).catch(() => {});
    });
    io.observe(el);
    return () => io.disconnect();
  });
</script>

<svg bind:this={el} viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" aria-label={label}>
  <path d={path} vector-effect="non-scaling-stroke"></path>
</svg>

<style>
  svg { display: block; width: 100%; height: 34px; }
  path { fill: var(--trace-fill); stroke: var(--trace); stroke-width: 1; }
</style>
