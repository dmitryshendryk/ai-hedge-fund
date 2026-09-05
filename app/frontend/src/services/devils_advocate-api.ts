const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8765';

export type Severity = 'none' | 'info' | 'warning' | 'critical';

export interface RedFlagFinding {
  detector: string;
  score: number;
  severity: Severity;
  headline: string;
  detail: Record<string, unknown>;
}

export interface RedFlagReport {
  ticker: string;
  score: number;
  severity: Severity;
  findings: RedFlagFinding[];
  disabled: boolean;
}

export interface DevilsAdvocateSettings {
  enabled: boolean;
}

class DevilsAdvocateService {
  private baseUrl = `${API_BASE_URL}/devils_advocate`;

  async getSettings(): Promise<DevilsAdvocateSettings> {
    const r = await fetch(`${this.baseUrl}/settings`);
    if (!r.ok) throw new Error(`devils_advocate settings fetch failed: ${r.statusText}`);
    return r.json();
  }

  async setSettings(enabled: boolean): Promise<DevilsAdvocateSettings> {
    const r = await fetch(`${this.baseUrl}/settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    if (!r.ok) throw new Error(`devils_advocate settings update failed: ${r.statusText}`);
    return r.json();
  }

  async getRedFlags(ticker: string): Promise<RedFlagReport> {
    const sym = ticker.trim().toUpperCase();
    const r = await fetch(`${this.baseUrl}/red_flags/${encodeURIComponent(sym)}`);
    if (!r.ok) throw new Error(`devils_advocate red_flags fetch failed for ${sym}: ${r.statusText}`);
    return r.json();
  }
}

export const devilsAdvocateService = new DevilsAdvocateService();
