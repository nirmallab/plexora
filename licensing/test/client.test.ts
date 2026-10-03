/**
 * CLIENT_JS is a template literal, so a regex escape written once inside it
 * reaches the browser without its backslash. Check the patterns as served.
 */
import { describe, expect, it } from 'vitest';
import { CLIENT_JS } from '../src/ui/client';

function pattern(marker: string): RegExp {
  const line = CLIENT_JS.split('\n').find((l) => l.includes(marker));
  const source = line?.match(/replace\(\/(.+?)\/g,/)?.[1];
  expect(source, marker).toBeTruthy();
  return new RegExp(source!, 'g');
}

describe('client script as served', () => {
  it('fills data-next placeholders from the response, dotted paths included', () => {
    const data = { model: { id: 'gemma-3-27b' } };
    const url = '/admin/ai/models/{model.id}'.replace(pattern('el.dataset.next.replace'), (_all, path: string) =>
      String(path.split('.').reduce((o: any, k) => o?.[k], data)));
    expect(url).toBe('/admin/ai/models/gemma-3-27b');
  });

  it('fills data-template placeholders from the form', () => {
    expect('/x/{id}/y'.replace(pattern('form.dataset.template.replace'), 'abc')).toBe('/x/abc/y');
  });
});
