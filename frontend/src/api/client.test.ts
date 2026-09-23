import { describe, expect, it } from 'vitest';
import { legacyRoute } from './client';

describe('bookmarks from the original workbench', () => {
  it('preserves the selected workspace and task review', () => {
    const project = 'a'.repeat(32), item = 'b'.repeat(32);
    expect(legacyRoute(`#projects/${project}/${item}`)).toBe(`/projects/${project}/workspaces/${item}`);
    expect(legacyRoute(`#tasks/${project}/${item}`)).toBe(`/projects/${project}/tasks/${item}`);
    expect(legacyRoute(`#review/${project}/${item}`)).toBe(`/projects/${project}/tasks/${item}/review`);
    expect(legacyRoute('#today')).toBe('/');
    expect(legacyRoute('#settings')).toBe('/settings');
  });
  it('does not interpret unknown fragments as resource identifiers', () => {
    for (const hash of ['#projects/../../settings', '#session=private', '#projects/%2f', '#https://foreign.example']) {
      expect(legacyRoute(hash)).toBeUndefined();
    }
  });
});
