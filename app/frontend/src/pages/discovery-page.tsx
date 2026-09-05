import { useEffect, useMemo, useState } from 'react';
import { Compass, ExternalLink, Loader2, RefreshCw, Star, Trash2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { TickerLink } from '@/components/ui/ticker-link';
import { useWatchlist } from '@/contexts/watchlist-context';
import { discoveryService, type DiscoveryConcentration, type DiscoveryIdea, type IdeaSignal, type MacroRegimeSnapshot } from '@/services/discovery-api';
import { devilsAdvocateService, type RedFlagReport, type Severity as DaSeverity } from '@/services/devils_advocate-api';
import { positionService, type PortfolioConcentration } from '@/services/position-api';
import { cn } from '@/lib/utils';

const HIGH_CONFLUENCE_MIN_SOURCES = 4;
const HIGH_CONFLUENCE_MIN_SCORE = 80;

type StrategyKey = 'all' | 'early_bird' | 'rockets' | 'compounders' | 'deep_value';

// Each preset filters to ideas that include at least one signal from the
// listed sources. "All" passes everything through unfiltered.
const STRATEGY_SOURCES: Record<Exclude<StrategyKey, 'all'>, Set<string>> = {
  early_bird: new Set([
    // Freshness-biased: things that fire BEFORE the move shows on a chart.
    // Insider Form 4 (2-day lag), SC 13D/G (10-day lag), spinoff filings,
    // government contract awards (signed today = revenue in 6mo), commodity
    // inflection. Excludes relative_strength and revenue_acceleration —
    // both lagging.
    'spinoff', 'activist_13d', 'csuite_buy', 'mega_dollar_buy',
    'insider_doubling_down', 'first_time_buyer', 'contrarian_setup',
    'commodity_tailwind', 'gov_contract_win', 'hiring_velocity',
    'share_cannibal',
  ]),
  rockets: new Set([
    'spinoff', 'squeeze', 'cluster_buy', 'mega_dollar_buy',
    'insider_doubling_down', 'first_time_buyer', 'repeat_buyer',
    'csuite_buy', 'revenue_acceleration', 'commodity_tailwind',
    'relative_strength', 'activist_13d', 'contrarian_setup',
    'vcp_breakout_setup',
  ]),
  compounders: new Set([
    'quality_score', 'high_roic', 'dividend_grower', 'analyst', 'csuite_buy',
    'share_cannibal', 'true_shareholder_yield', 'piotroski_score',
  ]),
  deep_value: new Set([
    'valuation_score', 'fcf_yield', 'contrarian_setup',
  ]),
};

const STRATEGY_TABS: { key: StrategyKey; label: string; title: string }[] = [
  { key: 'all', label: 'All', title: 'No source filter — every idea above SINGLE tier' },
  { key: 'early_bird', label: '🐣 Early Bird', title: 'Day-Zero signals: activist filings, insider buys, government contracts, hiring velocity — fires before the move hits the chart' },
  { key: 'rockets', label: '🚀 Rockets', title: 'Catalyst-driven momentum: insider clusters, spinoffs, squeezes, revenue inflection' },
  { key: 'compounders', label: '🐢 Compounders', title: 'Quality businesses to hold: high ROIC, durable margins, dividend growth, analyst upgrades' },
  { key: 'deep_value', label: '💎 Deep Value', title: 'Cheap businesses with insider validation: low PEG, high FCF yield, contrarian setups' },
];

interface ConfluenceTier {
  label: string;
  className: string;
  rowClassName: string;
}

function distinctSources(idea: DiscoveryIdea): number {
  return new Set(idea.signals.map((s) => s.source)).size;
}

const EXHAUSTION_THRESHOLD_PCT = 30;

function isExhausted(idea: DiscoveryIdea): boolean {
  return idea.pct_above_sma != null && idea.pct_above_sma > EXHAUSTION_THRESHOLD_PCT;
}

function confluenceTier(idea: DiscoveryIdea): ConfluenceTier {
  const sources = distinctSources(idea);
  // An extended name cannot reach the top tier however many signals agree: the
  // move already happened, so confluence overstates what is left to capture.
  if (sources >= HIGH_CONFLUENCE_MIN_SOURCES && idea.score >= HIGH_CONFLUENCE_MIN_SCORE && !isExhausted(idea)) {
    return {
      label: '🚨 SUPER-NOVA',
      className: 'border-destructive bg-destructive/20 text-destructive font-bold animate-pulse',
      rowClassName: 'bg-destructive/5 border-l-2 border-l-destructive',
    };
  }
  if (sources >= 3) {
    return {
      label: 'TRIPLE',
      className: 'border-primary/60 bg-primary/15 text-primary font-semibold',
      rowClassName: '',
    };
  }
  if (sources >= 2) {
    return {
      label: 'DOUBLE',
      className: 'border-primary/30 bg-primary/5 text-primary/80',
      rowClassName: '',
    };
  }
  return {
    label: 'SINGLE',
    className: 'border-muted-foreground/20 bg-muted/30 text-muted-foreground',
    rowClassName: '',
  };
}

// Non-intrusive Devil's Advocate badge. Renders nothing when the feature
// is disabled, the report hasn't loaded yet, or the ticker has no flags.
// This NEVER affects Discovery's score, sort, filter, or pagination.
function devilsAdvocateBadge(report: RedFlagReport | undefined) {
  if (!report || report.disabled || report.score <= 0 || report.findings.length === 0) {
    return <span className="text-muted-foreground/40 text-xs">—</span>;
  }
  const sev: DaSeverity = report.severity;
  const cls =
    sev === 'critical' ? 'border-destructive/60 bg-destructive/15 text-destructive font-semibold'
    : sev === 'warning' ? 'border-amber-500/50 bg-amber-500/15 text-amber-400 font-semibold'
    : 'border-muted-foreground/30 bg-muted/30 text-muted-foreground';
  const tooltip = report.findings.map((f) => `• ${f.headline}`).join('\n');
  const label =
    sev === 'critical' ? '⚠ CRITICAL'
    : sev === 'warning' ? '⚠ WARNING'
    : 'ℹ NOTE';
  return (
    <span
      className={cn('inline-flex items-center px-2 py-0.5 text-[10px] uppercase tracking-wider rounded border whitespace-nowrap font-data', cls)}
      title={tooltip}
    >
      {label} <span className="opacity-70 ml-1">{Math.round(report.score)}</span>
    </span>
  );
}

function signalBadge(s: IdeaSignal) {
  const cls =
    s.source === 'spinoff' ? 'border-primary/40 bg-primary/10 text-primary'
    : s.source === 'csuite_buy' ? 'border-amber-500/40 bg-amber-500/10 text-amber-400'
    : s.source === 'squeeze' ? 'border-destructive/40 bg-destructive/10 text-destructive'
    : s.source === 'cluster_buy' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400'
    : s.source === 'analyst' ? 'border-purple-500/40 bg-purple-500/10 text-purple-400'
    : s.source === 'commodity_tailwind' ? 'border-orange-500/40 bg-orange-500/10 text-orange-400'
    : s.source === 'insider_doubling_down' ? 'border-cyan-500/40 bg-cyan-500/10 text-cyan-400'
    : s.source === 'first_time_buyer' ? 'border-pink-500/40 bg-pink-500/10 text-pink-400'
    : s.source === 'mega_dollar_buy' ? 'border-yellow-500/40 bg-yellow-500/10 text-yellow-400'
    : s.source === 'repeat_buyer' ? 'border-teal-500/40 bg-teal-500/10 text-teal-400'
    : s.source === 'relative_strength' ? 'border-indigo-500/40 bg-indigo-500/10 text-indigo-400'
    : s.source === 'contrarian_setup' ? 'border-rose-500/40 bg-rose-500/10 text-rose-400 font-semibold'
    : s.source === 'activist_13d' ? 'border-fuchsia-500/40 bg-fuchsia-500/10 text-fuchsia-400 font-semibold'
    : s.source === 'revenue_acceleration' ? 'border-lime-500/40 bg-lime-500/10 text-lime-400 font-semibold'
    : s.source === 'quality_score' ? 'border-sky-500/40 bg-sky-500/10 text-sky-400'
    : s.source === 'valuation_score' ? 'border-green-500/40 bg-green-500/10 text-green-400'
    : s.source === 'dividend_grower' ? 'border-violet-500/40 bg-violet-500/10 text-violet-400'
    : s.source === 'fcf_yield' ? 'border-blue-500/40 bg-blue-500/10 text-blue-400 font-semibold'
    : s.source === 'high_roic' ? 'border-stone-400/40 bg-stone-400/10 text-stone-300 font-semibold'
    : s.source === 'gov_contract_win' ? 'border-lime-500/40 bg-lime-500/10 text-lime-300 font-semibold'
    : s.source === 'hiring_velocity' ? 'border-orange-500/40 bg-orange-500/10 text-orange-300'
    : s.source === 'share_cannibal' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300 font-semibold'
    : 'border-border bg-muted text-muted-foreground';
  return (
    <span
      key={`${s.source}-${s.label}`}
      className={cn('inline-flex items-center font-data text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border', cls)}
      title={`${s.source}: +${s.score}`}
    >
      {s.label} <span className="opacity-60 ml-1">+{s.score}</span>
    </span>
  );
}

/** Read-only badge: flags an idea whose sector or industry is already crowded
 *  in the user's book. Purely informational — never filters or reorders. */
function HeldExposureBadge({ idea, held }: { idea: DiscoveryIdea; held: PortfolioConcentration | null }) {
  if (!held) return null;
  const hit = [
    held.industries.find((b) => idea.industry && b.name === idea.industry),
    held.sectors.find((b) => idea.sector && b.name === idea.sector),
  ].find((b) => b && b.tier !== 'ok');
  if (!hit) return null;
  return (
    <span
      className={cn(
        'inline-flex items-center font-data text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border whitespace-nowrap',
        hit.tier === 'critical'
          ? 'border-destructive/50 bg-destructive/15 text-destructive'
          : 'border-amber-500/50 bg-amber-500/15 text-amber-400',
      )}
      title={`Your book is already ${hit.weight_pct.toFixed(0)}% ${hit.name} (${hit.tickers.join(', ')}). Adding this name deepens that exposure.`}
    >
      ⚠ {hit.weight_pct.toFixed(0)}% {hit.name}
    </span>
  );
}

export function DiscoveryPage() {
  const { isWatched, toggle } = useWatchlist();
  const [ideas, setIdeas] = useState<DiscoveryIdea[]>([]);
  const [concentration, setConcentration] = useState<DiscoveryConcentration | null>(null);
  const [macroRegime, setMacroRegime] = useState<MacroRegimeSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);
  const [cached, setCached] = useState(false);
  const [bulkLoading, setBulkLoading] = useState(false);
  const [flushing, setFlushing] = useState(false);
  const [dontChase, setDontChase] = useState(false);
  const [strategy, setStrategy] = useState<StrategyKey>('all');
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [totalUniverse, setTotalUniverse] = useState(0);

  // --- Devil's Advocate overlay (read-only, does NOT affect Discovery state) ---
  const [daEnabled, setDaEnabled] = useState<boolean | null>(null);  // null = unknown
  const [daReports, setDaReports] = useState<Record<string, RedFlagReport>>({});

  // --- Held-exposure overlay (read-only, does NOT affect Discovery state) ---
  // Concentration of the user's actual book, used only to badge ideas whose
  // sector or industry is already crowded. Never filters or reorders.
  const [heldConcentration, setHeldConcentration] = useState<PortfolioConcentration | null>(null);

  const PAGE_SIZE = 100;

  const load = async (filterEnabled = dontChase) => {
    setLoading(true);
    setError(null);
    try {
      const r = await discoveryService.getIdeas({
        page: 1,
        pageSize: PAGE_SIZE,
        maxAboveWhalePct: filterEnabled ? 20 : undefined,
      });
      setIdeas(r.ideas);
      setConcentration(r.concentration);
      setMacroRegime(r.macro_regime);
      setGeneratedAt(r.generated_at);
      setCached(r.cached);
      setPage(r.page);
      setTotalPages(r.total_pages);
      setHasMore(r.has_more);
      setTotalUniverse(r.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load ideas');
    } finally {
      setLoading(false);
    }
  };

  // Refresh alone re-serves the cached ranking for the rest of the TTL, so
  // forcing a recompute means discarding the server cache first.
  const handleFlushCache = async () => {
    setFlushing(true);
    try {
      const res = await discoveryService.flushCache();
      const hours = Math.round(res.cache_ttl_seconds / 3600);
      toast.success(
        res.total_entries > 0
          ? `Cache cleared — recomputing, next result holds for ${hours}h`
          : `Cache was already empty — recomputing, next result holds for ${hours}h`,
      );
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Cache flush failed');
    } finally {
      setFlushing(false);
    }
  };

  const loadMore = async () => {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    setError(null);
    try {
      const nextPage = page + 1;
      const r = await discoveryService.getIdeas({
        page: nextPage,
        pageSize: PAGE_SIZE,
        maxAboveWhalePct: dontChase ? 20 : undefined,
      });
      // Append; concentration + regime are universe-stable so don't replace them.
      setIdeas((prev) => [...prev, ...r.ideas]);
      setPage(r.page);
      setTotalPages(r.total_pages);
      setHasMore(r.has_more);
      setTotalUniverse(r.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load next page');
    } finally {
      setLoadingMore(false);
    }
  };

  const toggleDontChase = async () => {
    const next = !dontChase;
    setDontChase(next);
    await load(next);
  };

  useEffect(() => {
    load();
  }, []);

  // Held exposure: fetch the user's book concentration once on mount. Failure or
  // an empty book → no badges. A concentration outage must never degrade Discovery.
  useEffect(() => {
    let cancelled = false;
    positionService
      .list()
      .then((r) => { if (!cancelled) setHeldConcentration(r.concentration); })
      .catch(() => { if (!cancelled) setHeldConcentration(null); });
    return () => { cancelled = true; };
  }, []);

  // Devil's Advocate: fetch toggle state once on mount. Failure → leave disabled.
  useEffect(() => {
    let cancelled = false;
    devilsAdvocateService
      .getSettings()
      .then((s) => { if (!cancelled) setDaEnabled(s.enabled); })
      .catch(() => { if (!cancelled) setDaEnabled(false); });
    return () => { cancelled = true; };
  }, []);

  // When DA is enabled, batch-fetch red-flag reports for visible tickers that
  // we haven't fetched yet. Bounded concurrency: kick a handful at a time so
  // we don't slam the backend on the first render of a 100-row page.
  useEffect(() => {
    if (daEnabled !== true) return;
    const tickers = ideas
      .filter((i) => i.is_ticker)
      .map((i) => i.ticker.toUpperCase())
      .filter((t) => !(t in daReports));
    if (tickers.length === 0) return;

    let cancelled = false;
    const FANOUT = 4;
    let nextIndex = 0;

    const worker = async () => {
      while (!cancelled) {
        const i = nextIndex++;
        if (i >= tickers.length) return;
        const sym = tickers[i];
        try {
          const report = await devilsAdvocateService.getRedFlags(sym);
          if (cancelled) return;
          setDaReports((prev) => ({ ...prev, [sym]: report }));
        } catch {
          if (cancelled) return;
          // Mark as fetched-with-empty so we don't retry forever; the badge
          // renderer treats empty findings as "no flag".
          setDaReports((prev) => ({
            ...prev,
            [sym]: { ticker: sym, score: 0, severity: 'none', findings: [], disabled: false },
          }));
        }
      }
    };

    const workers = Array.from({ length: Math.min(FANOUT, tickers.length) }, () => worker());
    Promise.all(workers).catch(() => { /* swallowed per-ticker */ });
    return () => { cancelled = true; };
  }, [daEnabled, ideas, daReports]);

  const handleStarTopN = async (n: number = 10) => {
    setBulkLoading(true);
    let added = 0;
    let skippedCik = 0;
    let alreadyWatched = 0;

    for (const idea of ideas.slice(0, 30)) {
      if (added >= n) break;
      if (!idea.is_ticker) {
        skippedCik += 1;
        continue;
      }
      const t = idea.ticker.toUpperCase();
      if (isWatched(t)) {
        alreadyWatched += 1;
        continue;
      }
      try {
        await toggle(t);
        added += 1;
      } catch {
        // Continue with next
      }
    }
    setBulkLoading(false);

    const parts: string[] = [`Added ${added} to watchlist`];
    if (alreadyWatched > 0) parts.push(`${alreadyWatched} already watched`);
    if (skippedCik > 0) parts.push(`${skippedCik} CIK-only (no ticker yet)`);
    toast.success(parts.join(' · '));
  };

  const sortedIdeas = useMemo(
    () => [...ideas].sort((a, b) => b.score - a.score),
    [ideas],
  );

  const filteredIdeas = useMemo(() => {
    if (strategy === 'all') return sortedIdeas;
    const required = STRATEGY_SOURCES[strategy];
    return sortedIdeas.filter((idea) =>
      idea.signals.some((s) => required.has(s.source)),
    );
  }, [sortedIdeas, strategy]);

  const tickerCount = useMemo(() => ideas.filter((i) => i.is_ticker).length, [ideas]);

  return (
    <div className="flex-1 overflow-auto p-6 space-y-4">
      {/* Header */}
      <div className="space-y-2">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3">
            <Compass size={22} className="text-primary" />
            <h1 className="text-2xl font-bold text-foreground tracking-wide uppercase">Discovery</h1>
            <span className="text-[10px] font-data uppercase tracking-widest text-primary/70">
              // {ideas.length} ranked idea{ideas.length === 1 ? '' : 's'}
              {generatedAt && ` · generated ${new Date(generatedAt).toLocaleTimeString()}`}
              {cached && ' · cached'}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant={dontChase ? 'default' : 'outline'}
              size="sm"
              onClick={toggleDontChase}
              disabled={loading}
              className="gap-1.5"
              title={dontChase
                ? 'Showing only ideas within +20% of the cheapest price a tracked fund established its position at'
                : 'Filter to ideas within 20% of the cheapest price a tracked fund established its position at. This is 13F cost basis, often years old — a high number means a fund has held the name a long time, not that today\'s entry is poor.'}
            >
              🐋 Within 20% of whale cost basis
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => handleStarTopN(10)}
              disabled={bulkLoading || tickerCount === 0}
              className="gap-1.5"
              title="Add the top 10 ticker-bearing ideas to your watchlist"
            >
              {bulkLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Star className="h-3 w-3" />}
              Star top 10
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={handleFlushCache}
              disabled={loading || flushing}
              className="gap-1.5"
              title="Discard the cached ranking and recompute now. Ideas are cached for 4 hours, so Refresh alone re-serves the same list until it expires."
            >
              {flushing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
              Flush cache
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => load()}
              disabled={loading || flushing}
              className="gap-1.5"
            >
              {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
              Refresh
            </Button>
          </div>
        </div>
        <p className="text-sm text-muted-foreground">
          Ranked feed of tickers with active signals across spin-offs, C-suite insider buys, and squeeze setups.
          Click ⭐ to track. Click ticker to analyze.
        </p>
        <div className="hud-divider" />
      </div>

      {error && (
        <div className="border border-destructive/40 bg-destructive/10 text-destructive px-4 py-3 rounded-md text-sm">
          {error}
        </div>
      )}

      {!loading && ideas.length === 0 && !error && (
        <div className="border border-primary/30 bg-primary/5 text-foreground px-4 py-6 rounded-md text-sm space-y-1">
          <p className="font-medium">No ideas in the feed yet.</p>
          <p className="text-muted-foreground">
            Discovery aggregates from Catalysts (spin-offs), insider Form 4 buys, and short-squeeze candidates.
            Make sure <span className="font-data text-primary">FINNHUB_API_KEY</span>, <span className="font-data text-primary">EDGAR_IDENTITY</span>,
            and the spin-off sync are configured.
          </p>
        </div>
      )}

      {/* Macro regime banner — gates every Discovery score by FRED yield curve / VIX / HY OAS */}
      {macroRegime && (
        <section
          className={cn(
            'border rounded-md p-3 space-y-1 backdrop-blur-md',
            macroRegime.mode === 'risk_off'
              ? 'border-destructive/40 bg-destructive/10'
              : 'border-emerald-500/30 bg-emerald-500/5',
          )}
        >
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  'inline-flex items-center font-data text-[11px] uppercase tracking-wider px-2 py-0.5 rounded border font-semibold',
                  macroRegime.mode === 'risk_off'
                    ? 'border-destructive/50 bg-destructive/20 text-destructive'
                    : 'border-emerald-500/50 bg-emerald-500/20 text-emerald-300',
                )}
              >
                {macroRegime.mode === 'risk_off' ? '⚠ RISK-OFF' : '✓ RISK-ON'}
              </span>
              <span className="text-xs uppercase tracking-wider font-data text-muted-foreground">
                macro weather
              </span>
              {macroRegime.score_multiplier < 1 && (
                <span className="text-[11px] font-data text-amber-400">
                  scores ×{macroRegime.score_multiplier.toFixed(2)}
                </span>
              )}
            </div>
            <div className="flex items-center gap-3 text-[11px] font-data text-muted-foreground/80">
              {macroRegime.metrics.yield_curve_10y_2y != null && (
                <span title="10y - 2y Treasury spread">
                  Curve <span className={cn('font-semibold', (macroRegime.metrics.yield_curve_10y_2y ?? 0) < 0 ? 'text-amber-400' : 'text-foreground')}>
                    {(macroRegime.metrics.yield_curve_10y_2y ?? 0).toFixed(2)}%
                  </span>
                </span>
              )}
              {macroRegime.metrics.vix != null && (
                <span title="CBOE Volatility Index">
                  VIX <span className={cn('font-semibold', (macroRegime.metrics.vix ?? 0) > 25 ? 'text-amber-400' : 'text-foreground')}>
                    {(macroRegime.metrics.vix ?? 0).toFixed(1)}
                  </span>
                </span>
              )}
              {macroRegime.metrics.hy_oas != null && (
                <span title="ICE BofA US High Yield Option-Adjusted Spread">
                  HY OAS <span className={cn('font-semibold', (macroRegime.metrics.hy_oas ?? 0) > 5 ? 'text-amber-400' : 'text-foreground')}>
                    {(macroRegime.metrics.hy_oas ?? 0).toFixed(2)}%
                  </span>
                </span>
              )}
              {macroRegime.as_of && (
                <span className="opacity-60">as of {macroRegime.as_of}</span>
              )}
            </div>
          </div>
          {macroRegime.reasons.length > 0 && (
            <div className="text-[11px] font-data text-destructive/90">
              {macroRegime.reasons.join(' · ')}
            </div>
          )}
        </section>
      )}

      {/* Sector concentration HUD */}
      {concentration && concentration.sectors.length > 0 && (
        <section className="border border-primary/25 bg-card/40 backdrop-blur-md rounded-md p-3 space-y-2">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-2">
              <span className="text-xs uppercase tracking-wider font-data text-muted-foreground">Sector mix</span>
              <span className="text-[10px] font-data text-muted-foreground/70">
                top-50 cumulative score by sector
              </span>
            </div>
            {concentration.overcrowding_sectors.length > 0 && (
              <span className="inline-flex items-center gap-1 text-[11px] font-data uppercase tracking-wider px-2 py-0.5 rounded border border-amber-500/50 bg-amber-500/15 text-amber-400 font-semibold">
                ⚠ Overcrowding: {concentration.overcrowding_sectors.join(', ')} (&gt;{concentration.overcrowding_threshold_pct}%)
              </span>
            )}
          </div>
          <div className="space-y-1">
            {concentration.sectors.slice(0, 8).map((s) => {
              const isOver = concentration.overcrowding_sectors.includes(s.sector);
              const widthPct = Math.min(100, Math.max(2, s.score_pct));
              return (
                <div key={s.sector} className="flex items-center gap-2">
                  <div className="text-xs font-data w-28 truncate text-muted-foreground" title={s.sector}>
                    {s.sector}
                  </div>
                  <div className="flex-1 h-4 rounded bg-muted/30 overflow-hidden relative">
                    <div
                      className={`h-full ${isOver ? 'bg-amber-500/60' : 'bg-primary/50'}`}
                      style={{ width: `${widthPct}%` }}
                    />
                  </div>
                  <div className="text-xs font-data font-semibold w-12 text-right">
                    {s.score_pct.toFixed(0)}%
                  </div>
                  <div className="text-[10px] font-data text-muted-foreground/70 w-32 truncate" title={s.top_tickers.join(', ')}>
                    {s.top_tickers.slice(0, 3).join(', ')}
                  </div>
                </div>
              );
            })}
          </div>
          {concentration.unclassified_pct > 5 && (
            <p className="text-[10px] font-data text-muted-foreground/60 italic">
              {concentration.unclassified_pct.toFixed(0)}% of score from unclassified tickers (CIK-only spinoffs / untagged ADRs)
            </p>
          )}
        </section>
      )}

      {/* Strategy tabs */}
      {sortedIdeas.length > 0 && (
        <div className="flex items-center gap-1 border-b border-primary/25 -mb-px">
          {STRATEGY_TABS.map(({ key, label, title }) => {
            const active = strategy === key;
            const count = key === 'all'
              ? sortedIdeas.length
              : sortedIdeas.filter((i) => i.signals.some((s) => STRATEGY_SOURCES[key].has(s.source))).length;
            return (
              <button
                key={key}
                type="button"
                onClick={() => setStrategy(key)}
                title={title}
                className={cn(
                  'px-3 py-1.5 text-sm font-data tracking-wide border-b-2 transition-colors',
                  active
                    ? 'border-primary text-primary font-semibold'
                    : 'border-transparent text-muted-foreground hover:text-foreground',
                )}
              >
                {label}
                <span className="ml-1.5 text-[10px] text-muted-foreground/70">({count})</span>
              </button>
            );
          })}
        </div>
      )}

      {/* Table */}
      {sortedIdeas.length > 0 && (
        <div className="border border-primary/25 bg-card/60 backdrop-blur-md rounded-md overflow-hidden">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="uppercase text-xs tracking-wider w-12">Rank</TableHead>
                <TableHead className="uppercase text-xs tracking-wider w-28">Tier</TableHead>
                <TableHead className="uppercase text-xs tracking-wider w-32">Ticker</TableHead>
                <TableHead className="uppercase text-xs tracking-wider">Company</TableHead>
                <TableHead className="uppercase text-xs tracking-wider w-20 text-right">Score</TableHead>
                <TableHead className="uppercase text-xs tracking-wider w-24 text-right">30d / α</TableHead>
                <TableHead className="uppercase text-xs tracking-wider w-24 text-right">vs Whale</TableHead>
                {daEnabled && (
                  <TableHead className="uppercase text-xs tracking-wider w-28" title="Devil's Advocate — non-intrusive bear-thesis overlay (does not affect Discovery score)">
                    DA Flag
                  </TableHead>
                )}
                <TableHead className="uppercase text-xs tracking-wider">Signals</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredIdeas.map((idea, idx) => {
                const watched = idea.is_ticker && isWatched(idea.ticker);
                const tier = confluenceTier(idea);
                return (
                  <TableRow
                    key={`${idea.ticker}-${idx}`}
                    className={cn(watched && 'opacity-60', tier.rowClassName)}
                  >
                    <TableCell className="font-data text-sm text-muted-foreground">#{idx + 1}</TableCell>
                    <TableCell>
                      <span className={cn('inline-flex items-center font-data text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border whitespace-nowrap', tier.className)}>
                        {tier.label}
                      </span>
                      <HeldExposureBadge idea={idea} held={heldConcentration} />
                    </TableCell>
                    <TableCell>
                      {idea.is_ticker ? (
                        <TickerLink ticker={idea.ticker} />
                      ) : (
                        <Link
                          to="/catalysts"
                          className="inline-flex items-center gap-1 text-primary text-xs font-data hover:underline"
                          title={`Spin-off entity (no ticker yet) — view in Catalysts`}
                        >
                          CIK {idea.cik} <ExternalLink className="h-3 w-3" />
                        </Link>
                      )}
                    </TableCell>
                    <TableCell className="text-sm font-medium text-muted-foreground max-w-[300px] truncate" title={idea.company || ''}>
                      {idea.company || '—'}
                    </TableCell>
                    <TableCell className="font-data text-base text-right text-primary font-bold">
                      {Math.round(idea.score)}
                      {idea.exhaustion_penalty > 0 && (
                        <div
                          className="text-[10px] font-normal text-destructive"
                          title={`${idea.pct_above_sma?.toFixed(0)}% above its 200-day average — ${idea.exhaustion_penalty} points deducted and the top tier withheld`}
                        >
                          −{idea.exhaustion_penalty} exhausted
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-right font-data text-xs">
                      {idea.return_30d_pct == null ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        <div className="flex flex-col items-end">
                          <span className={cn(
                            'font-semibold',
                            idea.return_30d_pct > 0 ? 'text-primary' : idea.return_30d_pct < 0 ? 'text-destructive' : 'text-muted-foreground',
                          )}>
                            {idea.return_30d_pct > 0 ? '+' : ''}{idea.return_30d_pct.toFixed(1)}%
                          </span>
                          {idea.alpha_30d_pct != null && (
                            <span className={cn(
                              'text-[10px]',
                              idea.alpha_30d_pct > 0 ? 'text-primary/80' : idea.alpha_30d_pct < 0 ? 'text-destructive/80' : 'text-muted-foreground',
                            )}>
                              {idea.alpha_30d_pct > 0 ? '+' : ''}{idea.alpha_30d_pct.toFixed(1)} α
                            </span>
                          )}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-right font-data text-xs">
                      {idea.distance_from_whale_entry_pct == null ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        <span className={cn(
                          'font-semibold',
                          idea.distance_from_whale_entry_pct <= 0 ? 'text-primary'
                            : idea.distance_from_whale_entry_pct <= 20 ? 'text-primary/70'
                            : 'text-destructive',
                        )} title={`Current price vs lowest whale entry`}>
                          {idea.distance_from_whale_entry_pct > 0 ? '+' : ''}{idea.distance_from_whale_entry_pct.toFixed(0)}%
                        </span>
                      )}
                    </TableCell>
                    {daEnabled && (
                      <TableCell>
                        {devilsAdvocateBadge(idea.is_ticker ? daReports[idea.ticker.toUpperCase()] : undefined)}
                      </TableCell>
                    )}
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {idea.signals.map(signalBadge)}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      {/* Pagination footer — Path A: filters can shrink a page, but total
          reflects the unfiltered universe so Load More stays meaningful. */}
      {sortedIdeas.length > 0 && (
        <div className="flex items-center justify-between gap-3 flex-wrap text-xs font-data text-muted-foreground/80">
          <span>
            Showing <span className="text-foreground font-semibold">{ideas.length.toLocaleString()}</span>
            {' of '}
            <span className="text-foreground font-semibold">{totalUniverse.toLocaleString()}</span>
            {' ideas '}
            <span className="opacity-70">(page {page} of {totalPages})</span>
          </span>
          {hasMore ? (
            <button
              type="button"
              onClick={loadMore}
              disabled={loadingMore}
              className={cn(
                'px-3 py-1.5 rounded border border-primary/40 bg-primary/10 hover:bg-primary/20 text-primary font-data text-xs uppercase tracking-wider transition-colors',
                loadingMore && 'opacity-50 cursor-not-allowed',
              )}
            >
              {loadingMore ? 'Loading…' : `Load more (+${PAGE_SIZE})`}
            </button>
          ) : (
            <span className="opacity-70">End of ranked universe</span>
          )}
        </div>
      )}
    </div>
  );
}
