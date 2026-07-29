const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8765';

export interface Position {
  id: number;
  ticker: string;
  shares: number;
  cost_basis: number;
  entry_date: string;
  notes: string | null;
  cost_value: number;
  current_price: number | null;
  market_value: number | null;
  unrealized_pnl: number | null;
  unrealized_pnl_pct: number | null;
  return_since_entry_pct: number | null;
  alpha_pct_vs_spy: number | null;
  price_as_of: string | null;
}

export interface ConcentrationBucket {
  name: string;
  value: number;
  weight_pct: number;
  tickers: string[];
  tier: 'ok' | 'warn' | 'critical';
}

export interface PortfolioConcentration {
  total_value: number;
  valued_on: 'market' | 'mixed' | 'cost';
  sectors: ConcentrationBucket[];
  industries: ConcentrationBucket[];
  positions: ConcentrationBucket[];
  warnings: string[];
  unclassified_pct: number;
  sector_warn_pct: number;
  sector_critical_pct: number;
  industry_warn_pct: number;
  industry_critical_pct: number;
}

export interface ConcentrationPreviewResponse {
  ticker: string;
  amount: number;
  sector: string | null;
  industry: string | null;
  sector_weight_before_pct: number;
  sector_weight_after_pct: number;
  industry_weight_before_pct: number;
  industry_weight_after_pct: number;
  resulting_tier: 'ok' | 'warn' | 'critical';
  projected: PortfolioConcentration;
}

export interface PositionListResponse {
  items: Position[];
  total: number;
  total_cost_value: number;
  total_market_value: number | null;
  total_unrealized_pnl: number | null;
  total_unrealized_pnl_pct: number | null;
  concentration: PortfolioConcentration | null;
}

export interface PositionAddRequest {
  ticker: string;
  shares: number;
  cost_basis: number;
  entry_date?: string | null;
  notes?: string | null;
}

class PositionService {
  private baseUrl = `${API_BASE_URL}/positions`;

  async list(): Promise<PositionListResponse> {
    const r = await fetch(`${this.baseUrl}/`);
    if (!r.ok) throw new Error(`Failed to load positions: ${r.statusText}`);
    return r.json();
  }

  async add(req: PositionAddRequest): Promise<Position> {
    const r = await fetch(`${this.baseUrl}/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail || `Add failed: ${r.statusText}`);
    return r.json();
  }

  async remove(ticker: string): Promise<void> {
    const r = await fetch(`${this.baseUrl}/${encodeURIComponent(ticker)}`, { method: 'DELETE' });
    if (!r.ok) throw new Error(`Remove failed: ${r.statusText}`);
  }

  async previewConcentration(ticker: string, amount: number): Promise<ConcentrationPreviewResponse> {
    const q = new URLSearchParams({ ticker, amount: String(amount) });
    const r = await fetch(`${this.baseUrl}/concentration/preview?${q}`);
    if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail || `Preview failed: ${r.statusText}`);
    return r.json();
  }
}

export const positionService = new PositionService();
