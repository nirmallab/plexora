/**
 * Per-address limits on what costs something or could be abused: opening `/v1/ssh` (a TCP connection from Cloudflare
 * to a host the caller names) and creating a room (a page socket for a room with no stored auth). Both use Workers
 * Rate Limiting bindings (wrangler.toml `[[ratelimits]]`: SSH_LIMIT 20 a minute, ROOM_CREATE_LIMIT 30 a minute).
 *
 * The key is the client's address (`CF-Connecting-IP`); an IPv6 client is keyed by its /64, the block one subscriber
 * is usually given, so walking through its own addresses does not reset the count. The limits are per Cloudflare
 * location and approximate, which is enough to stop a script and never noticed by a person.
 *
 * A missing binding (an older local run) is no limit, and so is a binding that throws: the relay fails open rather than
 * refusing everyone over its own fault.
 */
import { parseIPv6 } from './hosts';

/** The address a limit counts against. */
export function clientKey(request: Request): string {
  const address = (request.headers.get('CF-Connecting-IP') ?? '').trim().toLowerCase();
  if (!address) return 'unknown';
  if (address.includes(':')) {
    const words = parseIPv6(address);
    if (words) return `${words.slice(0, 4).map((word) => word.toString(16)).join(':')}::/64`;
  }
  return address;
}

/** Whether `request` may go on under `limiter` (true when there is no limiter, or it fails). */
export async function withinLimit(limiter: RateLimit | undefined, request: Request): Promise<boolean> {
  if (!limiter || typeof limiter.limit !== 'function') return true;
  try {
    return (await limiter.limit({ key: clientKey(request) })).success;
  } catch {
    return true;
  }
}
