// The devices (DUTs) and which one is on the bench. Shared by the top bar, Live, Record and the Devices page.
import { api, type Device, type DeviceForm } from './api';

class Devices {
  list = $state<Device[]>([]);
  activeId = $state<number | null>(null);
  loaded = $state(false);
  active = $derived(this.list.find((d) => d.id === this.activeId) ?? null);

  async load() {
    const [list, active] = await Promise.all([api.devices(), api.activeDevice()]);
    this.list = list;
    this.activeId = active?.id ?? null;
    this.loaded = true;
  }

  /** Load once (the top bar does this at start); errors are left to whoever shows them. */
  ensure() {
    if (!this.loaded) this.load().catch(() => {});
  }

  async activate(id: number | null) {
    await api.setActiveDevice(id);
    this.activeId = id;
  }

  async save(form: DeviceForm, id?: number): Promise<Device> {
    const d = id == null ? await api.createDevice(form) : await api.updateDevice(id, form);
    await this.load();
    return d;
  }

  async remove(id: number) {
    await api.deleteDevice(id);
    await this.load();
  }
}

export const devices = new Devices();
