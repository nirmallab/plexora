/** Three machines each serving one part of a project to the viewer on your
 * computer: what a data node is, drawn. The flowing dashes and the travelling
 * dots are CSS, and stop under reduced motion. */
const sources = [
  { y: 24, title: 'HPC cluster', role: 'Image', file: '/scratch/tonsil.ome.tif' },
  { y: 164, title: 'University file server', role: 'Mask', file: 'sftp://files/cells.tif' },
  { y: 304, title: 'Notebook kernel', role: 'Cell table', file: 'adata, in memory' },
];
const paths = ['M232 66 C 300 66, 300 176, 356 176', 'M232 206 C 290 206, 300 206, 356 206', 'M232 346 C 300 346, 300 236, 356 236'];

export function NodeDiagram() {
  return (
    <svg
      viewBox="0 0 600 400"
      className="px-nodes"
      role="img"
      aria-label="An HPC cluster serves the image, a university file server the mask and a notebook kernel the cell table, all to one Plexora viewer on your computer."
    >
      <defs>
        <linearGradient id="px-flow" x1="0" x2="1" y1="0" y2="0">
          <stop offset="0" stopColor="#34c8f2" />
          <stop offset="1" stopColor="#8f4fa6" />
        </linearGradient>
        <radialGradient id="px-viewer-glow" cx="0.5" cy="0.5" r="0.6">
          <stop offset="0" stopColor="#38bdf8" stopOpacity="0.22" />
          <stop offset="1" stopColor="#38bdf8" stopOpacity="0" />
        </radialGradient>
      </defs>

      {paths.map((d, i) => (
        <g key={d}>
          <path d={d} className="px-nodes-track" />
          <path d={d} className="px-nodes-flow" style={{ animationDelay: `${i * -0.6}s` }} />
          <circle r="3.5" className="px-nodes-pulse" style={{ offsetPath: `path('${d}')`, animationDelay: `${i * 0.7}s` }} />
        </g>
      ))}

      {sources.map((s) => (
        <g key={s.title} transform={`translate(16 ${s.y})`}>
          <rect width="216" height="84" rx="12" className="px-nodes-card" />
          <circle cx="20" cy="22" r="4" className="px-nodes-live" />
          <text x="32" y="26" className="px-nodes-title">
            {s.title}
          </text>
          <text x="16" y="52" className="px-nodes-role">
            {s.role}
          </text>
          <text x="16" y="70" className="px-nodes-file">
            {s.file}
          </text>
        </g>
      ))}

      <ellipse cx="470" cy="206" rx="150" ry="130" fill="url(#px-viewer-glow)" />
      <g transform="translate(356 120)">
        <rect width="228" height="172" rx="14" className="px-nodes-viewer" />
        <g transform="translate(18 18)">
          <svg width="22" height="22" viewBox="0 -5 79 79" aria-hidden="true">
            <path fill="#4350ab" d="M77.5 6.15Q77.5 0 71.35 0Q65.2 0 60.16 3.53L8.87 39.44Q1.5 44.6 1.5 53.6L1.5 62.9Q1.5 69 7.6 69Q13.7 69 18.7 65.5L70.13 29.51Q77.5 24.35 77.5 15.35Z" />
            <path fill="#34c8f2" opacity=".86" d="M1.5 6.15Q1.5 0 7.65 0Q13.8 0 18.84 3.53L70.13 39.44Q77.5 44.6 77.5 53.6L77.5 62.9Q77.5 69 71.4 69Q65.3 69 60.3 65.5L8.87 29.51Q1.5 24.35 1.5 15.35Z" />
          </svg>
        </g>
        <text x="50" y="35" className="px-nodes-title">
          Your computer
        </text>
        <text x="18" y="72" className="px-nodes-role">
          One project
        </text>
        {['Image  ·  node hpc', 'Mask  ·  file server', 'Table  ·  this notebook'].map((line, i) => (
          <g key={line} transform={`translate(18 ${92 + i * 22})`}>
            <rect width="8" height="8" y="-7" rx="2" className="px-nodes-chip" style={{ opacity: 1 - i * 0.2 }} />
            <text x="16" y="0" className="px-nodes-file">
              {line}
            </text>
          </g>
        ))}
      </g>
    </svg>
  );
}
