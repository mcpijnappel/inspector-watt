// Hardware control by agents (MCP): requests waiting for an answer and the agent sessions that are allowed.
// Polled every 2 s while the app is open, so a request shows up quickly wherever you are.

import { api, type AgentState } from './api';

const POLL_MS = 2000;

/** "claude-code 2.1.300 (token: laptop)" -> "Claude Code (laptop)"; other clients keep their own name. */
export function clientName(client: string): string {
  const first = client.split(' ')[0] || 'An agent';
  const name = first === 'claude-code' ? 'Claude Code' : first;
  const token = client.match(/\(token: ([^)]+)\)/)?.[1];
  return token ? `${name} (${token})` : name;
}

class Agents {
  state = $state<AgentState | null>(null);
  error = $state('');
  private timer: ReturnType<typeof setInterval> | undefined;
  private started = false;

  start() {
    if (this.started) return;
    this.started = true;
    void this.poll();
    this.timer = setInterval(() => void this.poll(), POLL_MS);
  }

  stop() {
    clearInterval(this.timer);
    this.started = false;
  }

  async poll() {
    try {
      this.state = await api.agents();
      this.error = '';
    } catch (e) {
      this.error = (e as Error).message;
    }
  }

  async answer(id: number, allow: boolean) {
    try {
      await api.agentAnswer(id, allow);
    } catch (e) {
      this.error = (e as Error).message;
    }
    await this.poll();
  }

  async revoke(session: string | null = null) {
    try {
      await api.agentRevoke(session);
    } catch (e) {
      this.error = (e as Error).message;
    }
    await this.poll();
  }
}

export const agents = new Agents();
