// Svelte action: call `close` when the user clicks outside the element or presses Escape.
export function outside(node: HTMLElement, close: () => void) {
  let cb = close;
  const onDown = (e: PointerEvent) => {
    if (!node.contains(e.target as Node)) cb();
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape') cb();
  };
  // Defer so the click that opened the popover doesn't close it right away.
  const t = setTimeout(() => {
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey);
  });
  return {
    update(next: () => void) {
      cb = next;
    },
    destroy() {
      clearTimeout(t);
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey);
    },
  };
}
