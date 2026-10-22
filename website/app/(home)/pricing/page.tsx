import type { Metadata } from 'next';
import Link from 'next/link';
import { Box, Building2, Check, Clapperboard, Copy, CreditCard, Dna, KeyRound, Mail, Plug, Sparkles } from 'lucide-react';
import { SeatCalculator } from '@/components/seat-calculator';
import book from '@/data/pricebook.json';
import { accountUrl, licencesUrl, quoteUrl, trialUrl } from '@/lib/site';

// The two prices the page names in words, from the same book the calculator reads.
const money = (cents: number) => `$${(cents / 100).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
const academic = money(book.seatTiers['academic-seat'].price.introCents);
const commercial = money(book.seatTiers['commercial-seat'].price.introCents);

export const metadata: Metadata = {
  title: 'Pricing',
  description: `Plexora Free costs nothing. Plexora Pro is ${academic} per seat per year for academic use and ${commercial} for commercial use.`,
};

const pro = [
  { icon: Sparkles, title: 'Plexora AI', body: 'Guided gating sessions, AI quality control and an assistant that answers questions about your projects.' },
  { icon: Box, title: 'Plexora 3D', body: 'Surfaces, measuring and tracking cells in 3-D, whole-stack segmentation, Stack QC and 3-D colocalisation.' },
  { icon: Dna, title: 'Spatial transcriptomics', body: 'Xenium, Visium and Visium HD runs, with their transcript and bin layers and gene lists.' },
  { icon: Clapperboard, title: 'Video Studio', body: 'Videos made from the viewer: z-stack rotations, plane sweeps, time-lapses, zooms and channel reveals.' },
];

// What a buyer sees from the moment they pay: the order of these is the order it happens in.
const afterPaying = [
  {
    icon: CreditCard,
    title: 'Pay by card',
    body: 'Buy takes you to account.biocognia.com with your seats and licence type filled in: sign in with your email, choose Buy by card, and Paddle, the merchant of record, takes the payment and the tax. Plexora never sees a card.',
  },
  {
    icon: Mail,
    title: 'Your licence key arrives',
    body: 'Your own seat comes with its licence key, BIOC-XXXX-XXXX-XXXX-XXXX, by email within a minute of the payment.',
  },
  {
    icon: KeyRound,
    title: 'Paste it into Plexora',
    body: 'Settings › License › Licence key, or plexora license activate BIOC-… on the command line. The same key works on each of your computers.',
  },
  {
    icon: Copy,
    title: 'Copy it again any time',
    body: 'The key is always under Licences at account.biocognia.com: Show key, then Copy. Replace key makes a new one if it ever leaks.',
  },
];

export default function PricingPage() {
  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-14 px-4 py-12 md:px-6 md:py-16">
      <section className="grid items-start gap-10 lg:grid-cols-[1fr_minmax(0,26rem)]">
        <div className="flex flex-col gap-5">
          <p className="text-xs font-bold uppercase tracking-wide text-fd-primary">Pricing</p>
          <h1 className="text-3xl font-bold leading-tight md:text-4xl">Free for viewing. Plexora Pro when you need more.</h1>
          <p className="text-fd-muted-foreground md:text-lg">
            Plexora Free is everything Plexora does without a licence, for as long as you like: every image, table and
            modality, the manual tools, remote and HPC viewing, notebooks and export. Plexora Pro is {academic} per seat
            per year for academic use and {commercial} for commercial use, and adds:
          </p>
          <ul className="flex flex-col gap-3">
            {pro.map(({ icon: Icon, title, body }) => (
              <li key={title} className="flex gap-3">
                <Icon className="mt-0.5 size-5 shrink-0 text-fd-primary" aria-hidden="true" />
                <span>
                  <span className="font-semibold">{title}.</span>{' '}
                  <span className="text-fd-muted-foreground">{body}</span>
                </span>
              </li>
            ))}
            <li className="flex gap-3">
              <Plug className="mt-0.5 size-5 shrink-0 text-fd-muted-foreground" aria-hidden="true" />
              <span className="text-fd-muted-foreground">
                External MCP access, for an outside coding agent such as Claude Code, is off by default and is not part
                of Plexora Pro; an administrator can turn it on for your licence.
              </span>
            </li>
          </ul>
          <p className="flex items-center gap-2 text-sm text-fd-muted-foreground">
            <Check className="size-4 text-fd-primary" aria-hidden="true" />
            Each seat includes {book.notes.creditsPerSeat.toLocaleString('en-US')} AI credits a year; buy more whenever you
            need them.
          </p>
        </div>
        <SeatCalculator />
      </section>

      <section className="flex flex-col gap-4" aria-labelledby="after-paying">
        <h2 id="after-paying" className="text-xl font-semibold">
          After you pay
        </h2>
        <ol className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {afterPaying.map(({ icon: Icon, title, body }, index) => (
            <li key={title} className="flex flex-col gap-2 rounded-xl border border-fd-border bg-fd-card p-4">
              <span className="flex items-center gap-2">
                <Icon className="size-5 text-fd-primary" aria-hidden="true" />
                <span className="text-xs font-bold text-fd-muted-foreground">{index + 1}</span>
              </span>
              <span className="font-semibold">{title}</span>
              <span className="text-sm text-fd-muted-foreground">{body}</span>
            </li>
          ))}
        </ol>
        <p className="text-sm text-fd-muted-foreground">
          Bought more than one seat? Your account is now a team: invite people under Team › Members and give each a seat
          under Team › Seats. Each person gets their own key by email when given a seat. Lost an email?{' '}
          <a href={licencesUrl} className="font-medium text-fd-primary hover:underline">
            Your licences
          </a>{' '}
          show every key you hold. More in{' '}
          <Link href="/docs/licensing/buying" className="font-medium text-fd-primary hover:underline">
            Buying Plexora Pro
          </Link>
          .
        </p>
      </section>

      <section className="grid gap-3 md:grid-cols-3" aria-label="Other ways to start">
        <Link
          href="/docs/getting-started/free-and-pro"
          className="flex flex-col gap-1 rounded-xl border border-fd-border bg-fd-card p-4 transition-colors hover:border-fd-primary/50 hover:bg-fd-accent"
        >
          <span className="font-semibold">Free</span>
          <span className="text-2xl font-bold">$0</span>
          <span className="text-sm text-fd-muted-foreground">No account, no network, no time limit.</span>
        </Link>
        <a
          href={trialUrl}
          className="flex flex-col gap-1 rounded-xl border border-fd-border bg-fd-card p-4 transition-colors hover:border-fd-primary/50 hover:bg-fd-accent"
        >
          <span className="font-semibold">Trial</span>
          <span className="text-2xl font-bold">30 days</span>
          <span className="text-sm text-fd-muted-foreground">Everything in Plexora Pro with 100 AI credits. No card.</span>
        </a>
        <a
          href={quoteUrl}
          className="flex flex-col gap-1 rounded-xl border border-fd-border bg-fd-card p-4 transition-colors hover:border-fd-primary/50 hover:bg-fd-accent"
        >
          <span className="inline-flex items-center gap-2 font-semibold">
            <Building2 className="size-4 text-fd-primary" aria-hidden="true" /> Invoice or PO?
          </span>
          <span className="text-2xl font-bold">Request a quote</span>
          <span className="text-sm text-fd-muted-foreground">Pay by purchase order or bank transfer instead of a card.</span>
        </a>
      </section>

      <p className="border-t border-fd-border pt-6 text-sm text-fd-muted-foreground">
        Prices are in US dollars per seat per year, before tax. Accounts, licences and AI credits live on BioCognia, at{' '}
        <a href={accountUrl} className="hover:text-fd-foreground">
          account.biocognia.com
        </a>
        , shared with the lab&apos;s other tools.
      </p>
    </main>
  );
}
