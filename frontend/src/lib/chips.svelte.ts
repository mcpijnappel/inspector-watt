// The sensor library on the client: the chip types (loaded once, reloaded after a change), looked up by name.
import { api, type Chip, type ChipBus } from './api';

/** A chip's entry for one bus, if it talks on it. */
export const onBus = (c: Chip | undefined, bus: 'i2c' | 'spi'): ChipBus | undefined => c?.body.buses?.find((b) => b.bus === bus);

class Library {
  list = $state<Chip[]>([]);
  loaded = $state(false);
  byName = $derived(new Map(this.list.map((c) => [c.name.toLowerCase(), c])));
  /** Names (lower case) of the chip types that are I2C switches. */
  switchTypes = $derived(new Set(this.list.filter((c) => onBus(c, 'i2c')?.style === 'switch').map((c) => c.name.toLowerCase())));
  /** Chip types that talk SPI. */
  spi = $derived(this.list.filter((c) => !!onBus(c, 'spi')));
  private pending: Promise<void> | null = null;

  async load() {
    this.list = await api.chips();
    this.loaded = true;
  }

  ensure() {
    if (!this.loaded && !this.pending) this.pending = this.load().catch(() => {}).finally(() => (this.pending = null));
  }

  get(name: string | undefined | null): Chip | undefined {
    return name ? this.byName.get(name.toLowerCase()) : undefined;
  }

  /** Chip types that can answer at this address (for the "type" choices). */
  at(addr: number): Chip[] {
    return this.list.filter((c) => onBus(c, 'i2c')?.addresses?.includes(addr));
  }
}

export const library = new Library();
