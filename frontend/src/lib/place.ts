// Svelte action: keep an absolutely positioned popover inside the window.
// It opens under its anchor, right-aligned; if that runs off the left edge it flips to left-aligned, and on
// windows narrower than the popover it becomes a fixed sheet with a 12px margin on both sides.
const MARGIN = 12;

export function place(node: HTMLElement) {
  const fit = () => {
    const s = node.style;
    s.position = s.left = s.right = s.top = s.width = s.maxHeight = '';
    let r = node.getBoundingClientRect();
    if (r.width > window.innerWidth - 2 * MARGIN) {
      const top = Math.max(MARGIN, node.parentElement?.getBoundingClientRect().bottom ?? MARGIN) + 8;
      Object.assign(s, { position: 'fixed', left: `${MARGIN}px`, right: `${MARGIN}px`, top: `${top}px`, width: 'auto' });
      r = node.getBoundingClientRect();
    } else if (r.left < MARGIN) {
      Object.assign(s, { left: '0', right: 'auto' });
      r = node.getBoundingClientRect();
      if (r.right > window.innerWidth - MARGIN) s.left = `${window.innerWidth - MARGIN - r.right}px`;
      r = node.getBoundingClientRect();
    }
    s.maxHeight = `${Math.max(200, window.innerHeight - r.top - MARGIN)}px`;
    s.overflow = 'auto';
  };
  fit();
  window.addEventListener('resize', fit);
  return { destroy: () => window.removeEventListener('resize', fit) };
}
