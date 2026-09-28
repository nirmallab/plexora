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
import { Layout } from '../ui/layout';
import { adminApi } from './adminApi';
import { adminPages } from './adminPages';

export const admin = new Hono<AppEnv>();

admin.use('*', sameOriginGuard);

admin.get('/login', async (c) => {
  if (await adminIdentity(c)) return c.redirect('/admin');
  return page(c, (
    <Layout title="Administrator sign-in" product="admin">
      <div class="login card">
        <p class="muted">Behind Cloudflare Access this page is never needed. Otherwise sign in with ADMIN_TOKEN.</p>
        <form data-json action="/admin/login" data-next="/admin">
          <label>Admin token<input type="password" name="token" autocomplete="off" required /></label>
          <button class="primary" type="submit">Sign in</button>
        </form>
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
admin.route('/api', adminApi);
admin.use('*', requireAdmin);
admin.route('/', adminPages);
