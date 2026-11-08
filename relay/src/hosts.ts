/**
 * Which hosts `/v1/ssh` may open: a DNS name, or an IP literal outside the private, loopback, link-local and CGNAT
 * ranges (docs/internal/WEB_REMOTE_RELAY.md). Unspecified, multicast, broadcast and reserved addresses are refused with
 * them, as no SSH server lives there, and so are the IPv6 forms that carry an IPv4 address (mapped, NAT64, 6to4) when
 * that address is refused.
 *
 * A name is checked as written, not resolved: Cloudflare's `connect()` dials from the public internet. A name that
 * could be read as a number by some resolver (`127.1`, `0x7f.1`, `2130706433`) is not a DNS name and is refused unless
 * it is a dotted quad, which is then checked as an address.
 */

export type HostCheck = { ok: true; hostname: string } | { ok: false; reason: string };

const LABEL = /^(?!-)[a-z0-9-]{1,63}(?<!-)$/;

/** `host` as `connect()` should get it, or why it may not be opened. `allowPrivate` is the development switch. */
export function checkHost(host: string | null, allowPrivate = false): HostCheck {
  if (!host) return { ok: false, reason: 'host is required' };
  let name = host.trim().toLowerCase();
  if (name.length > 255) return { ok: false, reason: 'host is too long' };
  const bracketed = /^\[(.*)\]$/.exec(name);
  if (bracketed || name.includes(':')) {
    const literal = bracketed ? (bracketed[1] ?? '') : name;
    const words = parseIPv6(literal);
    if (!words) return { ok: false, reason: 'host is not a valid IPv6 address' };
    if (!allowPrivate && refusedIPv6(words)) return { ok: false, reason: 'host is a private or reserved address' };
    return { ok: true, hostname: literal };
  }
  if (name.endsWith('.')) name = name.slice(0, -1);
  const labels = name.split('.');
  const last = labels[labels.length - 1] ?? '';
  if (/^[0-9]+$/.test(last) || /^0x/.test(last)) {
    const octets = parseIPv4(name);
    if (!octets) return { ok: false, reason: 'host is not a valid IPv4 address' };
    if (!allowPrivate && refusedIPv4(octets)) return { ok: false, reason: 'host is a private or reserved address' };
    return { ok: true, hostname: name };
  }
  if (name.length > 253 || !labels.every((label) => LABEL.test(label))) {
    return { ok: false, reason: 'host is not a valid DNS name' };
  }
  if (!allowPrivate && (name === 'localhost' || name.endsWith('.localhost'))) {
    return { ok: false, reason: 'host is this machine' };
  }
  return { ok: true, hostname: name };
}

/** A strict dotted quad (four decimal octets, no leading zeros), else null. */
export function parseIPv4(text: string): number[] | null {
  const parts = text.split('.');
  if (parts.length !== 4) return null;
  const octets: number[] = [];
  for (const part of parts) {
    if (!/^(0|[1-9][0-9]{0,2})$/.test(part)) return null;
    const value = Number(part);
    if (value > 255) return null;
    octets.push(value);
  }
  return octets;
}

/** [first, prefix length] ranges no SSH server from the website may be on. */
const REFUSED_V4: ReadonlyArray<readonly [number[], number]> = [
  [[0, 0, 0, 0], 8], // "this network", unspecified
  [[10, 0, 0, 0], 8], // private
  [[100, 64, 0, 0], 10], // CGNAT
  [[127, 0, 0, 0], 8], // loopback
  [[169, 254, 0, 0], 16], // link-local
  [[172, 16, 0, 0], 12], // private
  [[192, 0, 0, 0], 24], // IETF protocol assignments
  [[192, 168, 0, 0], 16], // private
  [[198, 18, 0, 0], 15], // benchmarking
  [[224, 0, 0, 0], 4], // multicast
  [[240, 0, 0, 0], 4], // reserved, and 255.255.255.255
];

function v4Number(octets: readonly number[]): number {
  return (((octets[0] ?? 0) << 24) >>> 0) + ((octets[1] ?? 0) << 16) + ((octets[2] ?? 0) << 8) + (octets[3] ?? 0);
}

export function refusedIPv4(octets: readonly number[]): boolean {
  const value = v4Number(octets);
  return REFUSED_V4.some(([first, bits]) => {
    const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
    return ((value & mask) >>> 0) === ((v4Number(first) & mask) >>> 0);
  });
}

/** Eight 16-bit words, or null. Takes `::` compression and a trailing dotted quad; refuses a zone (`%eth0`). */
export function parseIPv6(text: string): number[] | null {
  if (!text || text.includes('%') || !/^[0-9a-f:.]+$/.test(text)) return null;
  let tail: number[] = [];
  let body = text;
  const lastColon = text.lastIndexOf(':');
  if (text.includes('.')) {
    const quad = parseIPv4(text.slice(lastColon + 1));
    if (!quad) return null;
    tail = [((quad[0] ?? 0) << 8) | (quad[1] ?? 0), ((quad[2] ?? 0) << 8) | (quad[3] ?? 0)];
    body = text.slice(0, lastColon + 1);
    // `…:a.b.c.d` leaves one colon to drop; `…::a.b.c.d` keeps its `::`.
    if (!body.endsWith('::')) body = body.slice(0, -1);
  }
  const halves = body.split('::');
  if (halves.length > 2) return null;
  const words = (part: string): number[] | null => {
    if (part === '') return [];
    const out: number[] = [];
    for (const group of part.split(':')) {
      if (!/^[0-9a-f]{1,4}$/.test(group)) return null;
      out.push(parseInt(group, 16));
    }
    return out;
  };
  const head = words(halves[0] ?? '');
  const rest = halves.length === 2 ? words(halves[1] ?? '') : [];
  if (!head || !rest) return null;
  const given = head.length + rest.length + tail.length;
  if (halves.length === 2) {
    if (given > 7) return null;
    return [...head, ...new Array<number>(8 - given).fill(0), ...rest, ...tail];
  }
  return given === 8 ? [...head, ...tail] : null;
}

function embeddedV4(words: readonly number[], at: number): number[] {
  const hi = words[at] ?? 0;
  const lo = words[at + 1] ?? 0;
  return [hi >> 8, hi & 0xff, lo >> 8, lo & 0xff];
}

export function refusedIPv6(words: readonly number[]): boolean {
  const w = (i: number) => words[i] ?? 0;
  const zeroUpTo = (n: number) => words.slice(0, n).every((word) => word === 0);
  if (zeroUpTo(8)) return true; // ::
  if (zeroUpTo(7) && w(7) === 1) return true; // ::1
  if (zeroUpTo(5) && w(5) === 0xffff) return refusedIPv4(embeddedV4(words, 6)); // ::ffff:a.b.c.d
  if (zeroUpTo(6)) return refusedIPv4(embeddedV4(words, 6)); // ::a.b.c.d (deprecated IPv4-compatible)
  if (w(0) === 0x64 && w(1) === 0xff9b && words.slice(2, 6).every((word) => word === 0)) {
    return refusedIPv4(embeddedV4(words, 6)); // 64:ff9b::/96 NAT64
  }
  if (w(0) === 0x2002) return refusedIPv4(embeddedV4(words, 1)); // 6to4
  if ((w(0) & 0xfe00) === 0xfc00) return true; // fc00::/7 unique local
  if ((w(0) & 0xffc0) === 0xfe80) return true; // fe80::/10 link-local
  if ((w(0) & 0xffc0) === 0xfec0) return true; // fec0::/10 site-local (deprecated)
  if ((w(0) & 0xff00) === 0xff00) return true; // ff00::/8 multicast
  return false;
}
