// App-wide preferences stored in the database (same on every device).
import { api, type Batteries } from './api';

const DEFAULT_BATTERIES: Batteries = { sizes_mah: [1000], selected_mah: 1000 };

class Settings {
  batteries = $state<Batteries>(DEFAULT_BATTERIES);
  private loaded = false;

  async load() {
    if (this.loaded) return;
    this.loaded = true;
    const b = await api.getSetting<Batteries>('batteries').catch(() => null);
    if (b?.sizes_mah?.length) this.batteries = b;
  }

  private async saveBatteries(b: Batteries) {
    this.batteries = b;
    await api.putSetting('batteries', b).catch(() => {});
  }

  selectBattery(mah: number) {
    void this.saveBatteries({ ...this.batteries, selected_mah: mah });
  }

  addBattery(mah: number) {
    if (!(mah > 0)) return;
    const sizes = [...new Set([...this.batteries.sizes_mah, Math.round(mah)])].sort((a, b) => a - b);
    void this.saveBatteries({ sizes_mah: sizes, selected_mah: Math.round(mah) });
  }

  removeBattery(mah: number) {
    const sizes = this.batteries.sizes_mah.filter((s) => s !== mah);
    if (!sizes.length) return;
    const selected = this.batteries.selected_mah === mah ? sizes[0] : this.batteries.selected_mah;
    void this.saveBatteries({ sizes_mah: sizes, selected_mah: selected });
  }
}

export const settings = new Settings();
