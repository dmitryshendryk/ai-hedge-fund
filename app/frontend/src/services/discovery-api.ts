const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8765';

export interface IdeaSignal {
  source: string;
  score: number;
  label: string;
  detail: Record<string, unknown> | null;
}

export interface DiscoveryIdea {
  ticker: string;
  company: string | null;
  cik: number | null;
  score: number;
  signals: IdeaSignal[];
  is_ticker: boolean;
  sector: string | null;
  industry: string | null;
  return_30d_pct: number | null;
  alpha_30d_pct: number | null;
  distance_from_whale_entry_pct: number | null;
}

export interface SectorBreakdown {
  sector: string;
  score_total: number;
  score_pct: number;
  ticker_count: number;
  top_tickers: string[];
}

export interface DiscoveryConcentration {
  sectors: SectorBreakdown[];
  overcrowding_threshold_pct: number;
  overcrowding_sectors: string[];
  unclassified_pct: number;
}

export interface MacroRegimeSnapshot {
  mode: 'risk_on' | 'risk_off' | string;
  score_multiplier: number;
  reasons: string[];
  metrics: { [key: string]: number | null };
  as_of: string | null;
}

export interface DiscoveryResponse {
  ideas: DiscoveryIdea[];
  total: number;
  cached: boolean;
  generated_at: string;
  concentration: DiscoveryConcentration | null;
  macro_regime: MacroRegimeSnapshot | null;
  page: number;
  page_size: number;
  total_pages: number;
  has_more: boolean;
}

export interface PageRequest {
  page?: number;        // 1-based
  pageSize?: number;    // default 100, max 200
  maxAboveWhalePct?: number;
}

class DiscoveryService {
  private baseUrl = `${API_BASE_URL}/discovery`;

  async getIdeas(req: PageRequest = {}): Promise<DiscoveryResponse> {
    const page = req.page ?? 1;
    const pageSize = req.pageSize ?? 100;
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (req.maxAboveWhalePct != null && req.maxAboveWhalePct > 0) {
      params.set('max_above_whale_pct', String(req.maxAboveWhalePct));
    }
    const r = await fetch(`${this.baseUrl}/ideas?${params.toString()}`);
    if (!r.ok) {
      const body = await r.json().catch(() => null);
      throw new Error(body?.detail || `Discovery fetch failed: ${r.statusText}`);
    }
    return r.json();
  }
}

export const discoveryService = new DiscoveryService();
