/**
 * Outbound email: seat keys, sign-in links, invitations, reminders.
 *
 * Through Resend when RESEND_API_KEY is set. Without it -- `wrangler dev`, the
 * test suite, a deployment that has not chosen a provider yet -- a message is
 * logged by subject and recipient (never its body, which may hold a key) and
 * kept in an in-memory outbox the tests read.
 */
import type { Env } from './env';

export interface Mail {
  to: string;
  subject: string;
  text: string;
}

export const outbox: Mail[] = [];

export function resetOutbox(): void {
  outbox.length = 0;
}

export async function send(env: Env, mail: Mail): Promise<boolean> {
  if (!env.RESEND_API_KEY) {
    outbox.push(mail);
    console.log(`[mail not sent: no provider] to=${mail.to} subject=${mail.subject}`);
    return false;
  }
  try {
    const response = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ from: env.MAIL_FROM ?? 'Plexora <licensing@plexora.example>', to: [mail.to],
        subject: mail.subject, text: mail.text }),
    });
    if (!response.ok) console.error('mail refused', response.status, mail.subject);
    return response.ok;
  } catch (error) {
    console.error('mail failed', mail.subject, String(error));
    return false;
  }
}

const SIGNATURE = '\n\n-- \nPlexora licensing. Your data never leaves your machine; licensing sees only your ' +
  'licence, seats and the names you give your environments.';

export function seatKeyMail(to: string, key: string, options: { trial?: boolean; expires_at: number; portal: string }): Mail {
  const until = new Date(options.expires_at * 1000).toISOString().slice(0, 10);
  const opener = options.trial
    ? `Your 30-day Plexora Paid trial is ready (until ${until}).`
    : `Your Plexora Paid seat is ready (licence valid until ${until}).`;
  return {
    to,
    subject: options.trial ? 'Your Plexora trial key' : 'Your Plexora seat key',
    text: `${opener}\n\nYour seat key:\n\n    ${key}\n\nActivate it in Plexora under Settings > License, or run:\n\n` +
      `    plexora license activate ${key}\n\nFor an HPC cluster, run that once on a login node with --cluster; ` +
      `every node and job then shares one registration.\n\nManage seats and environments: ${options.portal}` +
      '\n\nWhen a licence ends, everything Free keeps working and everything you made with Paid features stays ' +
      `yours.${SIGNATURE}`,
  };
}

export function loginMail(to: string, link: string, minutes: number): Mail {
  return {
    to,
    subject: 'Sign in to the Plexora licence portal',
    text: `Sign in to the Plexora licence portal:\n\n    ${link}\n\nThe link works once, for ${minutes} minutes. ` +
      `If you did not ask for it, ignore this message.${SIGNATURE}`,
  };
}

export function inviteMail(to: string, link: string, account: string): Mail {
  return {
    to,
    subject: `You have been given a Plexora seat (${account})`,
    text: `${account} has given you a Plexora Paid seat. Accept it here:\n\n    ${link}${SIGNATURE}`,
  };
}

export function reminderMail(to: string, kind: string, expiresAt: number, trial: boolean, portal: string): Mail {
  const until = new Date(expiresAt * 1000).toISOString().slice(0, 10);
  const what = trial ? 'Plexora trial' : 'Plexora licence';
  const when = kind.endsWith('_0') ? 'ends today' : `ends on ${until}`;
  return {
    to,
    subject: `Your ${what} ${when}`,
    text: `Your ${what} ${when}.\n\nPaid features (AI) stop when it ends${trial ? '' : ', after a grace period'}. ` +
      'Everything Free keeps working, and every gate, ROI, figure and export you made stays exactly as it is.' +
      `\n\n${trial ? 'Buy a licence' : 'Renew'}: ${portal}${SIGNATURE}`,
  };
}
