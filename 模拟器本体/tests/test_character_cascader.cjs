const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function loadFrontendScript() {
  const templatePath = path.resolve(__dirname, '..', 'web', 'templates', 'index.html');
  const html = fs.readFileSync(templatePath, 'utf8');
  const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
  const context = {
    console,
    document: { addEventListener() {} },
    fetch: () => new Promise(() => {}),
  };
  vm.createContext(context);
  vm.runInContext(script, context);
  return context;
}

test('characterGroups returns non-empty groups in the fixed path order', () => {
  const frontend = loadFrontendScript();
  assert.equal(typeof frontend.characterGroups, 'function');

  const characters = [
    { id: 'sparxie', name: '火花', element: '火', path: '欢愉', rarity: 5, max_energy: 120, completeness: 'full' },
    { id: 'fu_xuan', name: '符玄', element: '量子', path: '存护', rarity: 5, max_energy: 135, completeness: 'full' },
  ];

  const groups = JSON.parse(JSON.stringify(frontend.characterGroups(characters, '')));
  assert.deepEqual(groups, [
    { path: '存护', characters: [characters[1]] },
    { path: '欢愉', characters: [characters[0]] },
  ]);
});

test('characterGroups filters matches without flattening their path group', () => {
  const frontend = loadFrontendScript();
  const characters = [
    { id: 'sparxie', name: '火花', element: '火', path: '欢愉', rarity: 5, max_energy: 120, completeness: 'full' },
    { id: 'fu_xuan', name: '符玄', element: '量子', path: '存护', rarity: 5, max_energy: 135, completeness: 'full' },
    { id: 'sparkle', name: '花火', element: '量子', path: '同谐', rarity: 5, max_energy: 110, completeness: 'full' },
  ];

  const groups = JSON.parse(JSON.stringify(frontend.characterGroups(characters, 'sparx')));
  assert.deepEqual(groups, [
    { path: '欢愉', characters: [characters[0]] },
  ]);
});

test('resolveCharacterPath always returns a path that exists in the visible groups', () => {
  const frontend = loadFrontendScript();
  assert.equal(typeof frontend.resolveCharacterPath, 'function');
  const groups = [
    { path: '存护', characters: [{ id: 'fu_xuan' }] },
    { path: '欢愉', characters: [{ id: 'sparxie' }] },
  ];

  assert.equal(frontend.resolveCharacterPath(groups, 'sparxie', '虚无'), '欢愉');
  assert.equal(frontend.resolveCharacterPath(groups, 'missing', '虚无'), '存护');
  assert.equal(frontend.resolveCharacterPath([], 'sparxie', '欢愉'), '');
});
