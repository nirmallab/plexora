import type { ReactNode } from 'react';

// Building blocks of generated API and CLI pages. tools/docs/mdx.py emits
// these with JSON-encoded string attributes, so every prop here is a plain
// string or boolean and the prose arrives as children (already MDX).

export function ParamTable({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <div className="not-prose-headings">
      {title ? <p className="mb-2 mt-6 text-sm font-semibold text-fd-muted-foreground">{title}</p> : null}
      <div className="px-param-table">{children}</div>
    </div>
  );
}

export function Param({
  name,
  type,
  default: def,
  required,
  kind,
  children,
}: {
  name: string;
  type?: string;
  default?: string;
  required?: boolean;
  kind?: string;
  children?: ReactNode;
}) {
  return (
    <div className="px-param" id={`param-${name.replace(/[^A-Za-z0-9_-]/g, '')}`}>
      <div className="px-param-head">
        <code className="font-semibold text-fd-foreground">{name}</code>
        {type ? <code className="text-xs text-fd-primary">{type}</code> : null}
        {required ? <span className="px-chip px-chip-primary">required</span> : null}
        {kind ? <span className="px-chip">{kind}</span> : null}
        {def !== undefined ? (
          <span className="text-xs text-fd-muted-foreground">
            default <code>{def}</code>
          </span>
        ) : null}
      </div>
      {children ? <div className="px-param-body text-sm">{children}</div> : null}
    </div>
  );
}

export function Returns({ type, children }: { type?: string; children?: ReactNode }) {
  return (
    <div className="px-param-table">
      <div className="px-param">
        <div className="px-param-head">
          {type ? <code className="text-fd-primary">{type}</code> : <span className="text-fd-muted-foreground">Value</span>}
        </div>
        {children ? <div className="px-param-body text-sm">{children}</div> : null}
      </div>
    </div>
  );
}

export function Raises({ children }: { children: ReactNode }) {
  return <div className="px-param-table">{children}</div>;
}

export function Raise({ type, children }: { type: string; children?: ReactNode }) {
  return (
    <div className="px-param">
      <div className="px-param-head">
        <code className="font-semibold text-fd-foreground">{type}</code>
      </div>
      {children ? <div className="px-param-body text-sm">{children}</div> : null}
    </div>
  );
}

/** One member (method, property, attribute) of a documented class. */
export function Member({
  name,
  kind,
  signature,
  children,
}: {
  name: string;
  kind?: string;
  signature?: string;
  children?: ReactNode;
}) {
  return (
    <div className="my-4 rounded-xl border border-fd-border bg-fd-card px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <code className="font-semibold">{signature ?? name}</code>
        {kind ? <span className="px-chip">{kind}</span> : null}
      </div>
      {children ? <div className="text-sm [&>:last-child]:mb-0">{children}</div> : null}
    </div>
  );
}

export function Compare({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <div className="my-6 rounded-xl border border-fd-primary/30 bg-fd-primary/5 px-4 py-3">
      {title ? <p className="mt-0 font-semibold">{title}</p> : null}
      <div className="text-sm [&>:last-child]:mb-0">{children}</div>
    </div>
  );
}
