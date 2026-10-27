import Link from 'next/link';
import {
  ArrowRight,
  BookOpen,
  Cloud,
  CloudOff,
  Cpu,
  FolderOpen,
  HardDrive,
  FileLock2,
  Layers3,
  Monitor,
  NotebookPen,
  Server,
  Terminal,
  UserRoundX,
  WifiOff,
} from 'lucide-react';
import { PlexoraMark } from '@/components/brand';
import { CopyCommand } from '@/components/copy-command';
import { DownloadCard } from '@/components/download-card';
import { ExploreTile } from '@/components/explore-tile';
import { AppWindow } from '@/components/home/app-window';
import { NodeDiagram } from '@/components/home/node-diagram';
import { OsDownloadButton } from '@/components/home/os-download';
import { Reveal } from '@/components/home/reveal';
import { UiVideo } from '@/components/home/ui-video';
import { asset, demoVideo, downloads, pypiUrl, release, repoUrl } from '@/lib/site';

/** The image area of home/video-frame.webp, in its 2000x1250 pixels. */
const videoCanvas = { x: 594, y: 63, w: 1406, h: 1187 };

const presets = [
  { icon: Server, title: 'A plain SSH server', body: 'Any host you can already ssh into. Plexora runs there.' },
  { icon: Cpu, title: 'A Slurm cluster', body: 'Queues a job with your srun options and tunnels to the compute node.' },
  { icon: FolderOpen, title: 'Files on an SSH server', body: 'Nothing installed there. Images stream over SFTP.' },
  { icon: HardDrive, title: 'A workstation', body: 'Your own desktop or a lab machine: Windows, macOS or Linux.' },
  { icon: Cloud, title: 'Google Cloud', body: 'Mounts your Cloud Storage bucket on a VM and connects.' },
];

// The connection dialog's own steps for a Slurm server (connectionModal.js STEPS, node mode).
const stages = ['Reaching the machine', 'Signing in', 'Waiting for the scheduler', 'Opening the tunnel', 'Starting the data node'];

const principles = [
  { icon: CloudOff, title: 'Nothing to upload', body: 'Images are read where they are, a tile at a time.' },
  { icon: UserRoundX, title: 'No account', body: 'Open, analyse and export without signing in.' },
  { icon: WifiOff, title: 'Works offline', body: 'Runs with no network connection, air-gapped machines included.' },
  { icon: FileLock2, title: 'Originals untouched', body: 'Your source files change only when you ask.' },
];

const routes = [
  {
    icon: Monitor,
    title: 'On your computer',
    body: 'Installers for macOS, Windows and Linux, with everything bundled.',
    code: '.dmg  ·  .exe  ·  .deb',
    href: '/docs/getting-started/installation',
  },
  {
    icon: Terminal,
    title: 'From a terminal',
    body: 'The same app from Python 3.12 or 3.13, in your browser.',
    code: 'pip install plexora && plexora',
    href: '/docs/getting-started/install-python',
  },
  {
    icon: NotebookPen,
    title: 'In a notebook',
    body: 'AnnData, SpatialData or arrays in a Jupyter or VS Code cell, with no files written.',
    code: 'plexora.view("tonsil", adata=adata)',
    href: '/docs/notebook',
  },
  {
    icon: Server,
    title: 'On a cluster',
    body: 'One command queues a SLURM job and tunnels to it. Open OnDemand and JupyterHub are detected.',
    code: 'plexora connect me@login --srun "-p interactive"',
    href: '/docs/remote/slurm',
  },
];

const explore = [
  {
    title: 'Image viewer',
    body: 'Whole slides and z-stacks in 2-D or 3-D, up to 15 channels blended, each with its own colour and contrast.',
    href: '/docs/features/viewer',
    image: '/media/home/explore-viewer.webp',
  },
  {
    title: 'Plugins',
    body: 'Thresholding, regions, quality control, cell colouring and figures, open side by side on the image.',
    href: '/docs/plugins',
    image: '/media/home/explore-plugins.webp',
  },
  {
    title: 'Notebook',
    body: 'Show the objects already in your kernel, refresh them after an edit, keep working in Python.',
    href: '/docs/notebook',
    image: '/media/home/explore-notebook.webp',
  },
  {
    title: 'Python',
    body: 'Create projects, import samples and build datasets from a script or a pipeline.',
    href: '/docs/python',
    image: '/media/home/explore-python.webp',
  },
];

function Eyebrow({ children }: { children: React.ReactNode }) {
  return <p className="px-eyebrow">{children}</p>;
}

export default function HomePage() {
  return (
    <div className="px-home flex flex-1 flex-col">
      {/* Hero */}
      <section className="px-hero">
        <div className="px-hero-bg" aria-hidden="true" />
        <div className="relative mx-auto flex max-w-6xl flex-col items-center px-4 pt-16 text-center md:px-6 md:pt-24">
          <Reveal className="px-pill">
            <PlexoraMark size={16} />
            <span>Version {release.version}</span>
            <span className="px-pill-sep" aria-hidden="true" />
            <span>macOS · Windows · Linux</span>
          </Reveal>
          <Reveal as="h1" delay={60} className="px-hero-title">
            Multiplexed tissue images,
            <br />
            <span className="px-gradient-text">wherever they live.</span>
          </Reveal>
          <Reveal as="p" delay={120} className="px-hero-lead">
            Explore whole-slide and 3D multiplexed images from anywhere. Visualize, gate, annotate, and build figures—all
            without moving your data.
          </Reveal>
          <Reveal delay={180} className="mt-8 flex flex-wrap justify-center gap-3">
            <OsDownloadButton downloads={downloads} />
            <Link href="/docs/getting-started/installation" className="px-button px-button-secondary px-button-lg">
              <BookOpen className="size-4" aria-hidden="true" />
              Read the docs
            </Link>
          </Reveal>
          <Reveal delay={240} className="mt-5 flex flex-col items-center gap-2 text-sm text-fd-muted-foreground">
            <CopyCommand command="pip install plexora" />
          </Reveal>

          <Reveal delay={200} className="px-hero-shot">
            <AppWindow title="exemplar-001 — Plexora">
              <img
                src={asset('/media/getting-started/interface.webp')}
                alt="The Plexora viewer: a multiplexed tissue section, with its channels and contrast controls in the sidebar"
                width={2000}
                height={1250}
                fetchPriority="high"
              />
            </AppWindow>
          </Reveal>
        </div>
      </section>

      {/* Principles */}
      <section className="mx-auto w-full max-w-6xl px-4 py-16 md:px-6">
        <div className="grid gap-px overflow-hidden rounded-2xl border border-fd-border bg-fd-border sm:grid-cols-2 lg:grid-cols-4">
          {principles.map((p, i) => (
            <Reveal key={p.title} delay={i * 70} className="flex flex-col gap-2 bg-fd-background p-6">
              <p.icon className="size-5 text-fd-primary" aria-hidden="true" />
              <h3 className="font-semibold">{p.title}</h3>
              <p className="text-sm text-fd-muted-foreground">{p.body}</p>
            </Reveal>
          ))}
        </div>
      </section>

      {/* Remote servers */}
      <section className="px-section">
        <div className="mx-auto max-w-6xl px-4 md:px-6">
          <div className="mx-auto max-w-3xl text-center">
            <Reveal>
              <Eyebrow>Remote servers</Eyebrow>
            </Reveal>
            <Reveal as="h2" delay={60} className="px-section-title">
              Add a server. Open its images.
            </Reveal>
            <Reveal as="p" delay={120} className="px-section-lead mx-auto">
              Choose what kind of machine it is and give its address. Plexora connects with your own SSH and starts
              itself next to the data, on that machine or on a compute node, while you keep working in Plexora as
              usual. Your keys, your
              <code className="px-code-word">~/.ssh/config</code> and your Duo prompts work as they do in a terminal.
            </Reveal>
          </div>

          <div className="mt-12 grid items-start gap-8 lg:grid-cols-[0.85fr_1.15fr]">
            <div className="grid gap-3">
              {presets.map((p, i) => (
                <Reveal key={p.title} delay={i * 60}>
                  <div className="px-preset">
                    <span className="px-icon-tile">
                      <p.icon className="size-5" aria-hidden="true" />
                    </span>
                    <div>
                      <h3 className="font-semibold">{p.title}</h3>
                      <p className="text-sm text-fd-muted-foreground">{p.body}</p>
                    </div>
                  </div>
                </Reveal>
              ))}
              <Reveal delay={320}>
                <Link href="/docs/remote/add-a-server" className="px-link mt-2">
                  Add your first server <ArrowRight className="size-4" aria-hidden="true" />
                </Link>
              </Reveal>
            </div>

            <Reveal delay={120} className="flex min-w-0 flex-col gap-4">
              <AppWindow title="Add a server — Plexora">
                <img
                  src={asset('/media/remote/presets.webp')}
                  alt="Plexora's Add a server dialog, listing the kinds of machine it can connect to"
                  width={2000}
                  height={1250}
                  loading="lazy"
                  decoding="async"
                />
              </AppWindow>
              <ol className="px-stages" aria-label="What happens when you connect">
                {stages.map((s, i) => (
                  <li key={s} style={{ animationDelay: `${i * 0.9}s` }}>
                    <span aria-hidden="true" />
                    {s}
                  </li>
                ))}
              </ol>
            </Reveal>
          </div>
        </div>
      </section>

      {/* Data nodes: the signature */}
      <section className="px-section">
        <div className="mx-auto grid max-w-6xl items-center gap-12 px-4 md:px-6 lg:grid-cols-[0.9fr_1.1fr]">
          <div className="min-w-0">
            <Reveal>
              <Eyebrow>Data nodes</Eyebrow>
            </Reveal>
            <Reveal as="h2" delay={60} className="px-section-title">
              One project.
              <br />
              Many machines.
            </Reveal>
            <Reveal as="p" delay={120} className="px-section-lead">
              The image can stay on the cluster’s scratch space, the mask on your university’s file server, and the
              cell table in your notebook’s memory. Plexora reads each one where it is: through a data node, a Plexora
              with the viewer switched off, or straight over SFTP from a server where nothing else can run.
            </Reveal>
            <Reveal as="ul" delay={180} className="px-checks">
              <li>Image, mask and table are attached independently, each from any machine.</li>
              <li>Your project, regions and figures stay on your computer; only files are read remotely.</li>
              <li>A node can restart or go away without putting anything you made at risk.</li>
            </Reveal>
            <Reveal delay={240} className="mt-6">
              <div className="px-code">
                <span className="px-code-prompt">$</span> plexora node connect me@hpc \<br />
                <span className="pl-6">--serve image:tonsil=/scratch/tonsil.ome.tif</span>
              </div>
              <Link href="/docs/remote/data-nodes" className="px-link mt-4">
                How data nodes work <ArrowRight className="size-4" aria-hidden="true" />
              </Link>
            </Reveal>
          </div>
          <Reveal delay={120} className="px-panel min-w-0 p-4 md:p-6">
            <NodeDiagram />
          </Reveal>
        </div>
      </section>

      {/* Where it runs */}
      <section className="px-section">
        <div className="mx-auto max-w-6xl px-4 md:px-6">
          <div className="max-w-2xl">
            <Reveal>
              <Eyebrow>Runs where your data is</Eyebrow>
            </Reveal>
            <Reveal as="h2" delay={60} className="px-section-title">
              Four ways in. One viewer.
            </Reveal>
            <Reveal as="p" delay={120} className="px-section-lead">
              The desktop app, a terminal, a notebook cell and a compute node all open the same interface, with the
              same projects.
            </Reveal>
          </div>
          <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {routes.map((r, i) => (
              <Reveal key={r.title} delay={i * 80}>
                <Link href={r.href} className="px-card group flex h-full flex-col gap-3 p-5">
                  <span className="px-icon-tile">
                    <r.icon className="size-5" aria-hidden="true" />
                  </span>
                  <h3 className="font-semibold">{r.title}</h3>
                  <p className="text-sm text-fd-muted-foreground">{r.body}</p>
                  <code className="px-code-inline mt-auto">{r.code}</code>
                </Link>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* Video */}
      <section className="px-section">
        <div className="mx-auto max-w-6xl px-4 md:px-6">
          <div className="mx-auto max-w-2xl text-center">
            <Reveal>
              <Eyebrow>2-D and 3-D</Eyebrow>
            </Reveal>
            <Reveal as="h2" delay={60} className="px-section-title">
              Built for 2-D and 3-D.
            </Reveal>
            <Reveal as="p" delay={120} className="px-section-lead mx-auto">
              Whole slides, z-stacks and time series in the same viewer: step through planes, project a stack, or
              turn it in 3-D.
            </Reveal>
          </div>
          <Reveal delay={120} className="mx-auto mt-10 max-w-5xl">
            <AppWindow title="Plexora">
              <UiVideo
                frame="/media/home/video-frame.webp"
                canvas={videoCanvas}
                src={demoVideo.src}
                poster={demoVideo.poster}
                label="Plexora zooming from a whole multiplexed tissue section into a cropped region, then rotating that region as a 3-D volume"
              />
            </AppWindow>
          </Reveal>
        </div>
      </section>

      {/* Explore */}
      <section className="px-section">
        <div className="mx-auto max-w-6xl px-4 md:px-6">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div className="max-w-2xl">
              <Reveal>
                <Eyebrow>Explore Plexora</Eyebrow>
              </Reveal>
              <Reveal as="h2" delay={60} className="px-section-title">
                Everything on the image.
              </Reveal>
            </div>
            <Reveal delay={120}>
              <Link href="/docs" className="px-link">
                All documentation <ArrowRight className="size-4" aria-hidden="true" />
              </Link>
            </Reveal>
          </div>
          <div className="mt-10 grid gap-5 md:grid-cols-2">
            {explore.map((tile, i) => (
              <Reveal key={tile.href} delay={(i % 2) * 90}>
                <ExploreTile {...tile} />
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* Download */}
      <section id="download" className="px-section scroll-mt-16">
        <div className="mx-auto max-w-6xl px-4 md:px-6">
          <div className="mx-auto max-w-2xl text-center">
            <Reveal>
              <Eyebrow>Download</Eyebrow>
            </Reveal>
            <Reveal as="h2" delay={60} className="px-section-title">
              Get Plexora {release.version}
            </Reveal>
            <Reveal as="p" delay={120} className="px-section-lead mx-auto">
              Each installer includes everything Plexora needs. No Python required.
            </Reveal>
          </div>
          <div className="mx-auto mt-10 grid max-w-5xl gap-4 md:grid-cols-3">
            {downloads.map((d, i) => (
              <Reveal key={d.key} delay={i * 80} className="h-full">
                <DownloadCard download={d} />
              </Reveal>
            ))}
          </div>
          <Reveal as="p" delay={160} className="mx-auto mt-6 max-w-3xl text-center text-sm text-fd-muted-foreground">
            Another Linux, a server or a cluster? <code className="text-fd-foreground">pip install plexora</code>{' '}
            gives you the same app in your browser. See{' '}
            <Link href="/docs/getting-started/install-python" className="px-inline-link">
              Install with pip
            </Link>
            .
          </Reveal>
        </div>
      </section>

      {/* Closing call to action */}
      <section className="mx-auto w-full max-w-6xl px-4 pb-20 md:px-6">
        <Reveal className="px-cta">
          <div className="px-cta-bg" aria-hidden="true" />
          <Layers3 className="relative size-6 text-fd-primary" aria-hidden="true" />
          <h2 className="relative text-2xl font-semibold tracking-tight md:text-3xl">Open your first image in minutes.</h2>
          <p className="relative max-w-xl text-fd-muted-foreground">
            Install, point Plexora at an OME-TIFF, and add a mask and a cell table when you need them.
          </p>
          <div className="relative flex flex-wrap justify-center gap-3">
            <Link href="/docs/getting-started/first-image" className="px-button px-button-primary px-button-lg">
              Your first image <ArrowRight className="size-4" aria-hidden="true" />
            </Link>
            <a href="#download" className="px-button px-button-secondary px-button-lg">
              Download
            </a>
          </div>
        </Reveal>
      </section>

      <footer className="border-t border-fd-border">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-8 text-sm text-fd-muted-foreground md:px-6">
          <span className="inline-flex items-center gap-2 font-medium text-fd-foreground">
            <PlexoraMark size={16} /> Plexora
          </span>
          <Link href="/docs" className="hover:text-fd-foreground">
            Documentation
          </Link>
          <Link href="/docs/reference" className="hover:text-fd-foreground">
            Reference
          </Link>
          <a href={repoUrl} className="hover:text-fd-foreground">
            GitHub
          </a>
          <a href={pypiUrl} className="hover:text-fd-foreground">
            PyPI
          </a>
        </div>
      </footer>
    </div>
  );
}
