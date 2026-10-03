/** @jsxImportSource hono/jsx */
/**
 * /admin/ai: Plexora AI, as a four-step workflow and two quiet pages.
 *
 *   /admin/ai/providers    1  connect providers: keys, state, price lists, kill switches
 *   /admin/ai/models       2  approved models and the providers that reach each, in order;
 *                             /models/:id one model's advanced page (effort, prices by hand)
 *   /admin/ai/tasks        3  the general model, and which model does each task, by module
 *   /admin/ai              4  Overview: dismissible warnings, the summary, what serves now
 *   /admin/ai/usage           calls and cost by task, model, provider
 *   /admin/ai/settings        limits, credit, reliability, routing knobs
 *
 * /admin/ai/routing and /admin/ai/api, the old names of Tasks and of the keys
 * (now on Providers), redirect. The model of it: an approved model -> its
 * provider routes (up to three, tried in order) -> the tasks it is assigned
 * to. Every page reads the shared views (src/ai/views.ts) and changes things
 * only through /admin/api/ai.
 */
import { Hono } from 'hono';

import type { AppEnv } from '../../http';
import { modelPage, modelsPage } from './models';
import { overviewPage } from './overview';
import { providersPage } from './providers';
import { settingsPage } from './settings';
import { tasksPage } from './tasks';
import { usagePage } from './usage';

export const adminAi = new Hono<AppEnv>();

adminAi.get('/', overviewPage);
adminAi.get('/providers', providersPage);
adminAi.get('/models', modelsPage);
adminAi.get('/models/:id', modelPage);
adminAi.get('/tasks', tasksPage);
adminAi.get('/usage', usagePage);
adminAi.get('/settings', settingsPage);
adminAi.get('/routing', (c) => c.redirect(`/admin/ai/tasks${new URL(c.req.url).search}`, 301));
adminAi.get('/api', (c) => c.redirect('/admin/ai/providers', 301));
