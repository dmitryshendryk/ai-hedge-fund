import { useEffect, useMemo, useState } from 'react';
import { Briefcase, Loader2, Plus, X } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { TickerLink } from '@/components/ui/ticker-link';
import { positionService, type ConcentrationBucket, type PortfolioConcentration, type Position, type PositionListResponse } from '@/services/position-api';
import { cn } from '@/lib/utils';

function money(n: number | null | undefined): string {
  if (n == null) return '—';
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });
}

function pctClass(n: number | null | undefined): string {
  if (n == null) return 'text-muted-foreground';
  return n > 0 ? 'text-primary' : n < 0 ? 'text-destructive' : 'text-muted-foreground';
}

function signedPct(n: number | null | undefined): string {
  if (n == null) return '—';
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}%`;
}

const EMPTY_FORM = { ticker: '', shares: '', cost_basis: '', entry_date: '', notes: '' };

const TIER_BAR: Record<ConcentrationBucket['tier'], string> = {
  critical: 'bg-destructive/60',
  warn: 'bg-amber-500/60',
  ok: 'bg-primary/50',
};

function BucketBars({ label, buckets }: { label: string; buckets: ConcentrationBucket[] }) {
  if (buckets.length === 0) return null;
  return (
    <div className="space-y-1">
      <div className="text-[10px] uppercase tracking-wider font-data text-muted-foreground/70">{label}</div>
      {buckets.slice(0, 6).map((b) => (
        <div key={b.name} className="flex items-center gap-2">
          <div className="text-xs font-data w-44 truncate text-muted-foreground" title={b.name}>{b.name}</div>
          <div className="flex-1 h-4 rounded bg-muted/30 overflow-hidden">
            <div className={TIER_BAR[b.tier]} style={{ width: `${Math.min(100, Math.max(2, b.weight_pct))}%`, height: '100%' }} />
          </div>
          <div className="text-xs font-data font-semibold w-12 text-right">{b.weight_pct.toFixed(0)}%</div>
          <div className="text-[10px] font-data text-muted-foreground/70 w-32 truncate" title={b.tickers.join(', ')}>
            {b.tickers.slice(0, 3).join(', ')}
          </div>
        </div>
      ))}
    </div>
  );
}

function ConcentrationHud({ c }: { c: PortfolioConcentration }) {
  const worst = c.warnings.length > 0 ? (c.warnings[0].includes('critical') ? 'critical' : 'warn') : 'ok';
  return (
    <section
      className={cn(
        'border rounded-md p-3 space-y-3 bg-card/40 backdrop-blur-md',
        worst === 'critical' ? 'border-destructive/50' : worst === 'warn' ? 'border-amber-500/40' : 'border-primary/25',
      )}
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <span className="text-xs uppercase tracking-wider font-data text-muted-foreground">Concentration</span>
          <span className="text-[10px] font-data text-muted-foreground/70">
            factor exposure of {money(c.total_value)} held
          </span>
        </div>
        <span className="text-[10px] font-data text-muted-foreground/60">
          sector warn {c.sector_warn_pct}% / critical {c.sector_critical_pct}% &middot;
          industry warn {c.industry_warn_pct}% / critical {c.industry_critical_pct}%
        </span>
      </div>

      {c.warnings.length > 0 && (
        <div className="space-y-1">
          {c.warnings.slice(0, 4).map((w) => (
            <div
              key={w}
              className={cn(
                'text-[11px] font-data uppercase tracking-wider px-2 py-0.5 rounded border font-semibold inline-block mr-2',
                w.includes('critical')
                  ? 'border-destructive/50 bg-destructive/15 text-destructive'
                  : 'border-amber-500/50 bg-amber-500/15 text-amber-400',
              )}
            >
              ⚠ {w}
            </div>
          ))}
        </div>
      )}

      <BucketBars label="By sector" buckets={c.sectors} />
      <BucketBars label="By industry" buckets={c.industries} />

      {c.valued_on !== 'market' && (
        <p className="text-[10px] font-data text-muted-foreground/60 italic">
          {c.valued_on === 'cost'
            ? 'No holding could be priced — weights use cost basis.'
            : 'Some holdings could not be priced — those contribute at cost basis.'}
        </p>
      )}
      {c.unclassified_pct > 5 && (
        <p className="text-[10px] font-data text-muted-foreground/60 italic">
          {c.unclassified_pct.toFixed(0)}% unclassified (broad-market ETFs carry no sector, and an index sleeve is not a factor bet)
        </p>
      )}
    </section>
  );
}

export function PositionsPage() {
  const [data, setData] = useState<PositionListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [adding, setAdding] = useState(false);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await positionService.list());
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load positions');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    const shares = parseFloat(form.shares);
    const costBasis = parseFloat(form.cost_basis);
    if (!form.ticker.trim() || !Number.isFinite(shares) || !Number.isFinite(costBasis)) {
      toast.error('Ticker, shares, and cost basis are required');
      return;
    }
    setAdding(true);
    try {
      await positionService.add({
        ticker: form.ticker.trim().toUpperCase(),
        shares,
        cost_basis: costBasis,
        entry_date: form.entry_date || null,
        notes: form.notes || null,
      });
      toast.success(`Added ${form.ticker.trim().toUpperCase()}`);
      setForm({ ...EMPTY_FORM });
      await load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Add failed');
    } finally {
      setAdding(false);
    }
  };

  const handleRemove = async (ticker: string) => {
    try {
      await positionService.remove(ticker);
      toast.success(`Removed ${ticker}`);
      await load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Remove failed');
    }
  };

  const items = useMemo(() => data?.items ?? [], [data]);

  return (
    <div className="flex-1 overflow-auto p-6 space-y-4">
      {/* Header + portfolio roll-up */}
      <div className="space-y-2">
        <div className="flex items-center gap-3 flex-wrap">
          <Briefcase size={22} className="text-primary" />
          <h1 className="text-2xl font-bold text-foreground tracking-wide uppercase">Positions</h1>
          <span className="text-[10px] font-data uppercase tracking-widest text-primary/70">
            // {items.length} holding{items.length === 1 ? '' : 's'}
          </span>
        </div>
        <p className="text-sm text-muted-foreground">
          Your holdings with live price and unrealized P&amp;L. Adds cost basis so exit alerts
          (<span className="text-primary">stop_loss</span>, <span className="text-primary">technical_breakdown</span>)
          can act on names you actually own.
        </p>
        {data && (
          <div className="flex items-center gap-6 flex-wrap font-data text-sm pt-1">
            <div>
              <span className="text-muted-foreground text-xs uppercase tracking-wider mr-2">Cost</span>
              {money(data.total_cost_value)}
            </div>
            <div>
              <span className="text-muted-foreground text-xs uppercase tracking-wider mr-2">Market</span>
              {money(data.total_market_value)}
            </div>
            <div>
              <span className="text-muted-foreground text-xs uppercase tracking-wider mr-2">Unrealized</span>
              <span className={cn('font-semibold', pctClass(data.total_unrealized_pnl))}>
                {money(data.total_unrealized_pnl)}
                {data.total_unrealized_pnl_pct != null && ` (${signedPct(data.total_unrealized_pnl_pct)})`}
              </span>
            </div>
          </div>
        )}
        <div className="hud-divider" />
      </div>

      {/* Concentration HUD — factor exposure of owned capital */}
      {data?.concentration && <ConcentrationHud c={data.concentration} />}

      {/* Add form */}
      <form onSubmit={handleAdd} className="flex items-end gap-2 flex-wrap border border-primary/25 bg-card/60 backdrop-blur-md rounded-md p-3">
        <div className="flex flex-col gap-1">
          <label className="text-[10px] uppercase tracking-wider text-muted-foreground">Ticker</label>
          <Input value={form.ticker} onChange={(e) => setForm({ ...form, ticker: e.target.value })} placeholder="AMD" className="w-28 uppercase" />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[10px] uppercase tracking-wider text-muted-foreground">Shares</label>
          <Input value={form.shares} onChange={(e) => setForm({ ...form, shares: e.target.value })} placeholder="10" type="number" step="any" className="w-24" />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[10px] uppercase tracking-wider text-muted-foreground">Cost / share</label>
          <Input value={form.cost_basis} onChange={(e) => setForm({ ...form, cost_basis: e.target.value })} placeholder="120.50" type="number" step="any" className="w-28" />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[10px] uppercase tracking-wider text-muted-foreground">Entry date</label>
          <Input value={form.entry_date} onChange={(e) => setForm({ ...form, entry_date: e.target.value })} type="date" className="w-40" />
        </div>
        <div className="flex flex-col gap-1 flex-1 min-w-[120px]">
          <label className="text-[10px] uppercase tracking-wider text-muted-foreground">Notes</label>
          <Input value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} placeholder="optional" />
        </div>
        <Button type="submit" disabled={adding} className="gap-1.5">
          {adding ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
          Add
        </Button>
      </form>

      {error && (
        <div className="border border-destructive/40 bg-destructive/10 text-destructive px-4 py-3 rounded-md text-sm">
          {error}
        </div>
      )}

      {/* Table */}
      <div className="border border-primary/25 bg-card/60 backdrop-blur-md rounded-md overflow-hidden">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="uppercase text-xs tracking-wider">Ticker</TableHead>
              <TableHead className="uppercase text-xs tracking-wider text-right">Shares</TableHead>
              <TableHead className="uppercase text-xs tracking-wider text-right">Cost / sh</TableHead>
              <TableHead className="uppercase text-xs tracking-wider text-right">Price</TableHead>
              <TableHead className="uppercase text-xs tracking-wider text-right">Market Value</TableHead>
              <TableHead className="uppercase text-xs tracking-wider text-right">Unrealized P&amp;L</TableHead>
              <TableHead className="uppercase text-xs tracking-wider text-right">Stop</TableHead>
              <TableHead className="uppercase text-xs tracking-wider text-right">vs SPY</TableHead>
              <TableHead className="uppercase text-xs tracking-wider w-12" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {loading && items.length === 0 ? (
              <TableRow>
                <TableCell colSpan={8} className="text-center py-12 text-muted-foreground">
                  <Loader2 className="h-5 w-5 animate-spin inline-block mr-2" />
                  Loading positions...
                </TableCell>
              </TableRow>
            ) : items.length === 0 ? (
              <TableRow>
                <TableCell colSpan={8} className="text-center py-12 text-muted-foreground">
                  <p>No positions yet.</p>
                  <p className="text-xs mt-2">Add a holding above with its ticker, share count, and average cost.</p>
                </TableCell>
              </TableRow>
            ) : (
              items.map((p: Position) => (
                <TableRow key={p.ticker}>
                  <TableCell><TickerLink ticker={p.ticker} hideStar /></TableCell>
                  <TableCell className="text-right font-data text-xs">{p.shares}</TableCell>
                  <TableCell className="text-right font-data text-xs">{money(p.cost_basis)}</TableCell>
                  <TableCell className="text-right font-data text-xs">
                    {p.current_price == null ? <span className="text-muted-foreground">—</span> : money(p.current_price)}
                  </TableCell>
                  <TableCell className="text-right font-data text-xs">{money(p.market_value)}</TableCell>
                  <TableCell className="text-right font-data text-xs">
                    {p.unrealized_pnl == null ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <div className="flex flex-col items-end">
                        <span className={cn('font-semibold', pctClass(p.unrealized_pnl))}>{money(p.unrealized_pnl)}</span>
                        <span className={cn('text-[10px]', pctClass(p.unrealized_pnl_pct))}>{signedPct(p.unrealized_pnl_pct)}</span>
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-data text-xs">
                    {p.stop_loss_price == null ? (
                      <span className="text-muted-foreground" title="No price history, so no stop was set">—</span>
                    ) : (
                      <div
                        className="flex flex-col items-end"
                        title={`${p.stop_multiple}x ATR (${p.stop_atr?.toFixed(2)}) below your cost basis. Below this level the technical trade is invalid.`}
                      >
                        <span className="font-semibold">{money(p.stop_loss_price)}</span>
                        {p.distance_to_stop_pct != null && (
                          <span
                            className={cn(
                              'text-[10px]',
                              p.distance_to_stop_pct <= 0
                                ? 'text-destructive font-semibold'
                                : p.distance_to_stop_pct < 5
                                  ? 'text-amber-400'
                                  : 'text-muted-foreground',
                            )}
                          >
                            {p.distance_to_stop_pct <= 0 ? 'BREACHED' : `${p.distance_to_stop_pct.toFixed(1)}% away`}
                          </span>
                        )}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-data text-xs">
                    <span className={cn(pctClass(p.alpha_pct_vs_spy))} title="Return since entry vs SPY">
                      {signedPct(p.alpha_pct_vs_spy)}
                    </span>
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0 hover:text-destructive"
                      onClick={() => handleRemove(p.ticker)}
                      title="Remove position"
                    >
                      <X className="h-3 w-3" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
