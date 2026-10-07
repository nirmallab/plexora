import Link from 'next/link';
import {
  BookOpen,
  Cable,
  CodeXml,
  Dna,
  Microscope,
  NotebookPen,
  Puzzle,
  Server,
  ServerCog,
} from 'lucide-react';
import { asset, pypiUrl, repoUrl, startUrl, tagline, version } from '@/lib/site';

const tasks = [
  {
    title: 'Open multiplexed imaging data',
    body: 'OME-TIFF, OME-Zarr and whole-slide images with cell tables and segmentation masks.',
    href: '/docs/modalities/multiplexed-imaging',
    icon: Microscope,
  },
  {
    title: 'View spatial transcriptomics',
    body: 'Xenium, Visium and Visium HD runs, detected and imported from the output folder.',
    href: '/docs/modalities/xenium',
    icon: Dna,
  },
  {
    title: 'Use Plexora from Python',
    body: 'Create projects, import samples and open the viewer from a script.',
    href: '/docs/python',
    icon: CodeXml,
  },
  {
    title: 'Use Plexora in a notebook',
    body: 'Show AnnData, SpatialData, arrays and data frames in Jupyter or VS Code.',
    href: '/docs/notebooks',
    icon: NotebookPen,
  },
  {
    title: 'Run on a remote workstation',
    body: 'Keep the data where it is and view it through an SSH tunnel.',
    href: '/docs/remote/ssh-connect',
    icon: Server,
  },
  {
    title: 'Run on an HPC cluster',
    body: 'SLURM, Open OnDemand and JupyterHub, with the viewer in your browser.',
    href: '/docs/remote/slurm',
    icon: ServerCog,
  },
  {
    title: 'Read data from another machine',
    body: 'Data nodes serve files from where they live to the viewer on your laptop.',
    href: '/docs/remote/data-nodes',
    icon: Cable,
  },
  {
    title: 'Build a plugin',
    body: 'Add a sidebar tool with a Python descriptor, routes and browser code.',
    href: '/docs/plugin-development',
    icon: Puzzle,
  },
];

export default function HomePage() {
  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-12 px-4 py-12 md:px-6 md:py-16">
      <section className="grid items-center gap-10 lg:grid-cols-[1.05fr_1fr]">
        <div className="flex flex-col gap-5">
          <p className="text-xs font-bold uppercase tracking-wide text-fd-primary">Plexora {version}</p>
          <h1 className="text-3xl font-bold leading-tight md:text-4xl">
            A viewer for multiplexed imaging and spatial transcriptomics
          </h1>
          <p className="text-fd-muted-foreground md:text-lg">{tagline}</p>
          <div className="flex flex-wrap gap-3">
            <Link
              href="/docs/getting-started/quick-start"
              className="rounded-lg bg-fd-primary px-4 py-2 text-sm font-semibold text-fd-primary-foreground"
            >
              Quick start
            </Link>
            <Link
              href="/docs/getting-started/installation"
              className="rounded-lg border border-fd-border bg-fd-card px-4 py-2 text-sm font-semibold"
            >
              Install
            </Link>
            <Link
              href="/docs/python-api"
              className="rounded-lg border border-fd-border bg-fd-card px-4 py-2 text-sm font-semibold"
            >
              Python API
            </Link>
          </div>
          <pre className="w-fit max-w-full overflow-x-auto rounded-lg border border-fd-border bg-fd-card px-4 py-3 text-sm">
            <code>pip install plexora{'\n'}plexora</code>
          </pre>
        </div>
        <img
          src={asset('/screenshots/home/viewer.webp')}
          alt="The Plexora viewer showing a multiplexed tissue image with cell outlines"
          className="w-full rounded-xl border border-fd-border shadow-lg"
        />
      </section>

      <section className="flex flex-col gap-4">
        <h2 className="text-xl font-semibold">What do you want to do?</h2>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {tasks.map(({ title, body, href, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              className="group flex flex-col gap-2 rounded-xl border border-fd-border bg-fd-card p-4 transition-colors hover:border-fd-primary/50 hover:bg-fd-accent"
            >
              <Icon className="size-5 text-fd-primary" aria-hidden="true" />
              <span className="font-semibold">{title}</span>
              <span className="text-sm text-fd-muted-foreground">{body}</span>
            </Link>
          ))}
        </div>
      </section>

      <section className="flex flex-wrap items-center gap-x-6 gap-y-2 border-t border-fd-border pt-6 text-sm text-fd-muted-foreground">
        <Link href="/docs" className="inline-flex items-center gap-1.5 hover:text-fd-foreground">
          <BookOpen className="size-4" aria-hidden="true" /> All documentation
        </Link>
        <a href={repoUrl} className="hover:text-fd-foreground">
          GitHub
        </a>
        <a href={pypiUrl} className="hover:text-fd-foreground">
          PyPI
        </a>
        <a href={startUrl} className="hover:text-fd-foreground">
          Sign in or sign up
        </a>
      </section>
    </main>
  );
}
