'use client';

// The pricing page's calculator: use class and seats in, the price per seat and
// the total out, and Buy, which goes to the portal's Plans & prices with the
// seats and use class filled in, where Paddle's checkout opens. Every number
// comes from data/pricebook.json, the website's copy of the platform's prices
// (tools/sync_pricing.py keeps it right); nothing here is a price. One price per
// seat: the total is that price times the seats.
import { useMemo, useState } from 'react';
import { ArrowRight, Info, Minus, Plus } from 'lucide-react';
import book from '@/data/pricebook.json';
import { buyHref, trialUrl, type UseClass } from '@/lib/site';

type Price = { introCents: number; standardCents: number };
type SeatTier = { name: string; audience: UseClass; machinesPerSeat: number; price: Price };

const TIERS = book.seatTiers as unknown as Record<string, SeatTier>;
const TIER_KEYS = Object.keys(TIERS);
// An individual, the smallest team, and two lab sizes; the stepper takes any number.
const PRESETS = [1, 2, 5, 10];

function money(cents: number): string {
  return `$${(cents / 100).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;

export function SeatCalculator() {
  const [tierKey, setTierKey] = useState(TIER_KEYS[0]);
  const [seats, setSeats] = useState(1);

  const quote = useMemo(() => {
    const tier = TIERS[tierKey];
    return {
      tier,
      unit: tier.price.introCents,
      standardUnit: tier.price.standardCents,
      total: tier.price.introCents * seats,
      standardTotal: tier.price.standardCents * seats,
    };
  }, [tierKey, seats]);

  const setCount = (value: number) => setSeats(Math.max(1, Math.floor(Number.isFinite(value) ? value : 1)));

  return (
    <section
      aria-label="Plexora Pro price calculator"
      className="flex flex-col gap-5 rounded-xl border border-fd-border bg-fd-card p-5 shadow-sm md:p-6"
    >
      <div role="tablist" aria-label="Licence type" className="flex w-fit rounded-lg border border-fd-border p-1">
        {TIER_KEYS.map((key) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={key === tierKey}
            onClick={() => setTierKey(key)}
            className={`rounded-md px-3 py-1.5 text-sm font-semibold transition-colors ${
              key === tierKey ? 'bg-fd-primary text-fd-primary-foreground' : 'text-fd-muted-foreground hover:text-fd-foreground'
            }`}
          >
            {TIERS[key].name}
          </button>
        ))}
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-bold uppercase tracking-wide text-fd-muted-foreground">Seats</span>
          <div className="flex items-center rounded-lg border border-fd-border">
            <button
              type="button"
              aria-label="One seat fewer"
              disabled={seats <= 1}
              onClick={() => setCount(seats - 1)}
              className="p-2 text-fd-muted-foreground hover:text-fd-foreground disabled:opacity-40"
            >
              <Minus className="size-4" aria-hidden="true" />
            </button>
            <input
              aria-label="Number of seats"
              type="number"
              min={1}
              value={seats}
              onChange={(event) => setCount(Number(event.target.value))}
              className="w-14 bg-transparent text-center text-lg font-semibold tabular-nums outline-none"
            />
            <button
              type="button"
              aria-label="One seat more"
              onClick={() => setCount(seats + 1)}
              className="p-2 text-fd-muted-foreground hover:text-fd-foreground"
            >
              <Plus className="size-4" aria-hidden="true" />
            </button>
          </div>
        </div>
        <div className="flex gap-1.5" aria-label="Seat presets">
          {PRESETS.map((preset) => (
            <button
              key={preset}
              type="button"
              onClick={() => setCount(preset)}
              className={`rounded-md border px-2.5 py-1 text-sm tabular-nums ${
                seats === preset
                  ? 'border-fd-primary bg-fd-accent font-semibold text-fd-foreground'
                  : 'border-fd-border text-fd-muted-foreground hover:text-fd-foreground'
              }`}
            >
              {preset}
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-col gap-1.5 border-t border-fd-border pt-5" aria-live="polite">
        <p className="flex items-baseline gap-2">
          <span className="text-4xl font-bold tabular-nums">{money(quote.unit)}</span>
          <span className="text-fd-muted-foreground">per seat / year</span>
        </p>
        <p className="flex items-center gap-1.5 text-sm text-fd-muted-foreground">
          Launch price. Standard: <s>{money(quote.standardUnit)}</s>
          <span title="The price for new subscriptions once launch pricing ends. A subscription bought now keeps its launch price for as long as it runs.">
            <Info className="size-3.5" aria-hidden="true" />
          </span>
        </p>
        <p className="text-sm">
          {plural(seats, 'seat')} · <strong>{money(quote.total)}</strong> a year{' '}
          <s className="text-fd-muted-foreground">{money(quote.standardTotal)}</s>
        </p>
        <p className="text-sm text-fd-muted-foreground">{book.notes.renewal}</p>
      </div>

      <div className="flex flex-col gap-3">
        <a
          href={buyHref(seats, quote.tier.audience)}
          className="inline-flex items-center justify-center gap-2 rounded-lg bg-fd-primary px-4 py-2.5 text-sm font-semibold text-fd-primary-foreground"
        >
          Buy {plural(seats, 'seat')} · {money(quote.total)}/year
          <ArrowRight className="size-4" aria-hidden="true" />
        </a>
        <p className="text-center text-xs text-fd-muted-foreground">
          One seat is an individual account; two or more make a team you can invite people to.
        </p>
        <a href={trialUrl} className="text-center text-sm font-medium text-fd-primary hover:underline">
          Or start a 30-day free trial, no card
        </a>
      </div>

      <p className="text-xs text-fd-muted-foreground">
        Each seat is one person, on up to {quote.tier.machinesPerSeat} computers, with{' '}
        {book.notes.creditsPerSeat.toLocaleString('en-US')} AI credits a year.
      </p>
    </section>
  );
}
