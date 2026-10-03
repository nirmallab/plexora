/** @jsxImportSource hono/jsx */
/**
 * /admin: sign-in, the JSON API (adminApi.ts) and the pages (adminPages.tsx).
 * Everything but /admin/login sits behind `requireAdmin`; every mutation
 * behind `sameOriginGuard`.
 */
import { Hono } from 'hono';

import { adminIdentity, checkAdminToken, clearAdminCookie, mintAdminCookie, requireAdmin, setAdminCookie } from '../adminAuth';
import { nowSeconds } from '../env';
import { ApiError, type AppEnv, ok, page, readJson, sameOriginGuard, str } from '../http';
import { Field, JsonForm, Mark } from '../ui/components';
import { Layout } from '../ui/layout';
import { adminApi } from './adminApi';
import { aiAdmin } from './ai';
import { aiCatalogAdmin } from './aiAdminCatalog';
import { adminAi } from './adminAi/index';
import { adminPages } from './adminPages';

export const admin = new Hono<AppEnv>();

admin.use('*', sameOriginGuard);

admin.get('/login', async (c) => {
  if (await adminIdentity(c)) return c.redirect('/admin');
  return page(c, (
    <Layout title="Administrator sign-in" product="admin" heading={false} center>
      <div class="auth-card">
        <div class="auth-badge"><Mark /></div>
        <h1>Admin</h1>
        <p class="lede">Licence administration for Plexora. With Cloudflare Access in front of /admin this page is
          never needed; otherwise sign in with the ADMIN_TOKEN secret.</p>
        <JsonForm action="/admin/login" next="/admin" submit="Sign in" wideSubmit>
          <Field label="Admin token" name="token" type="password" required autocomplete="off"
            hint="Sessions last 12 hours." />
        </JsonForm>
      </div>
    </Layout>
  ) as unknown as string);
});

admin.post('/login', async (c) => {
  const body = await readJson(c);
  const token = str(body, 'token', 512);
  if (!token || !(await checkAdminToken(c, token))) {
    throw new ApiError(401, 'unauthorized', 'That token is not the admin token.');
  }
  const cookie = await mintAdminCookie(c, 'token', nowSeconds());
  if (!cookie) throw new ApiError(503, 'internal_error', 'SESSION_KEY is not configured.');
  setAdminCookie(c, cookie);
  return ok(c, { ok: true });
});

admin.post('/logout', async (c) => {
  clearAdminCookie(c);
  return ok(c, { ok: true });
});

admin.use('/api/*', requireAdmin);
admin.route('/api/ai', aiAdmin);
admin.route('/api/ai', aiCatalogAdmin);
admin.route('/api', adminApi);
admin.use('*', requireAdmin);
admin.route('/ai', adminAi);
admin.route('/', adminPages);
