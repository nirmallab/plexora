'use client';

import { Check, Copy } from 'lucide-react';
import { useState } from 'react';

/** A one-line shell command with a copy button. */
export function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="inline-flex max-w-full items-center gap-2 rounded-lg border border-fd-border bg-fd-card py-1 pl-3 pr-1 font-mono text-sm">
      <span aria-hidden="true" className="select-none text-fd-muted-foreground">
        $
      </span>
      <code className="truncate">{command}</code>
      <button
        type="button"
        className="rounded-md p-1.5 text-fd-muted-foreground transition-colors hover:bg-fd-accent hover:text-fd-foreground"
        aria-label={copied ? 'Copied' : `Copy ${command}`}
        onClick={() => {
          navigator.clipboard?.writeText(command).then(
            () => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            },
            () => {},
          );
        }}
      >
        {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
      </button>
    </div>
  );
}
