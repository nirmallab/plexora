/** @jsxImportSource hono/jsx */
/**
 * /admin/ai: Plexora AI, in six pages.
 *
 *   /admin/ai              Overview: problems, cost, what serves now
 *   /admin/ai/models       approved models; /models/:id one model's provider routes
 *   /admin/ai/providers    keys, price lists, kill switches
 *   /admin/ai/routing      which model does each task, by module
 *   /admin/ai/usage        calls and cost by task, model, provider
 *   /admin/ai/settings     limits, credit, reliability, routing knobs
 *
 * The model of it: an approved model -> its provider routes (up to three,
 * tried in order) -> the tasks it is assigned to. Every page reads the
 * shared views (src/ai/views.ts) and changes things only through
 * /admin/api/ai.
 */
import { Hono } from 'hono';

import type { AppEnv } from '../../http';
import { modelPage, modelsPage } from './models';
import { overviewPage } from './overview';
import { providersPage } from './providers';
import { routingPage } from './routing';
import { settingsPage } from './settings';
import { usagePage } from './usage';

export const adminAi = new Hono<AppEnv>();

adminAi.get('/', overviewPage);
adminAi.get('/models', modelsPage);
adminAi.get('/models/:id', modelPage);
adminAi.get('/providers', providersPage);
adminAi.get('/routing', routingPage);
adminAi.get('/usage', usagePage);
adminAi.get('/settings', settingsPage);
