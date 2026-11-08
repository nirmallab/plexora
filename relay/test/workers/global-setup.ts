/**
 * Starts the node-side fixtures (test/fixtures.ts) once for the Workers project and hands their ports and the test
 * certificates to vitest.workers.config.ts (bindings) and to the tests (`inject`).
 */
import type { TestProject } from 'vitest/node';

import { startFixtures } from '../fixtures';

export default async function setup(project: TestProject) {
  const fixtures = await startFixtures();
  const { close, ...provided } = fixtures;
  project.provide('fixtures', provided);
  return close;
}

declare module 'vitest' {
  export interface ProvidedContext {
    fixtures: Omit<import('../fixtures').Fixtures, 'close'>;
  }
}
