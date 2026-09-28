import defaultMdxComponents from 'fumadocs-ui/mdx';
import { Tab, Tabs } from 'fumadocs-ui/components/tabs';
import { Step, Steps } from 'fumadocs-ui/components/steps';
import { File, Files, Folder } from 'fumadocs-ui/components/files';
import { Accordion, Accordions } from 'fumadocs-ui/components/accordion';
import type { MDXComponents } from 'mdx/types';
import { Compare, Member, Param, ParamTable, Raise, Raises, Returns } from './mdx/api';
import { Badge, InternalBadge, ModalityBadge, PluginBadge } from './mdx/badges';
import { Screenshot } from './mdx/media';
import { PlatformTabs, WhereTabs } from './mdx/tabs';
import { SourceLink } from './mdx/source-link';

// Every component a page may use, so no .mdx file imports anything.
export function getMDXComponents(components?: MDXComponents) {
  return {
    ...defaultMdxComponents,
    Tab,
    Tabs,
    Step,
    Steps,
    File,
    Files,
    Folder,
    Accordion,
    Accordions,
    Compare,
    Member,
    Param,
    ParamTable,
    Raise,
    Raises,
    Returns,
    Badge,
    InternalBadge,
    ModalityBadge,
    PluginBadge,
    Screenshot,
    PlatformTabs,
    WhereTabs,
    SourceLink,
    ...components,
  } satisfies MDXComponents;
}

export const useMDXComponents = getMDXComponents;

declare global {
  type MDXProvidedComponents = ReturnType<typeof getMDXComponents>;
}
