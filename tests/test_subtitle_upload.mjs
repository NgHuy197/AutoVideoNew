import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes, createRequire } from 'node:module'
import { test } from 'node:test'
import { pathToFileURL } from 'node:url'

// Test the same TypeScript helpers used by upload/resume, with Node's built-in
// type stripping. No browser state, network, or third-party test runner needed.
const frontend = new URL('../frontend/', import.meta.url)
const require = createRequire(new URL('package.json', frontend))
const dataModule = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const styleSource = stripTypeScriptTypes(await readFile(new URL('src/subtitleStyle.ts', frontend), 'utf8'))
const styleUrl = dataModule(styleSource)
const style = await import(styleUrl)
const uploadSource = stripTypeScriptTypes(await readFile(new URL('src/upload.ts', frontend), 'utf8'))
  .replace("'./subtitleStyle'", JSON.stringify(styleUrl))
  .replace("'@noble/hashes/sha256'", JSON.stringify(pathToFileURL(require.resolve('@noble/hashes/sha256')).href))
const { presetKey, isUploadSession } = await import(dataModule(uploadSource))
const preset = { audio_mode: 'replace', subtitle_mode: 'burn', include_dubbing: true }

test('old upload resumes with default style and equivalent reordered style', () => {
  assert.equal(presetKey(preset), presetKey({ ...preset, subtitle_style: { ...style.DEFAULT_SUBTITLE_STYLE } }))
  const first = { color: '#aabbcc', font_family: ' Arial ', alignment: 8 }
  const second = { alignment: 8, font_family: 'Arial', color: '#AABBCC' }
  assert.equal(presetKey({ ...preset, subtitle_style: first }), presetKey({ ...preset, subtitle_style: second }))
})

test('every style change gets a separate upload identity', () => {
  const changes = { font_family: 'Arial', color: '#123456', outline_color: '#123456', font_size: 8,
    outline_width: 5, shadow: 4, alignment: 8, margin_v: 10, margin_h: 10,
    bold: true, italic: true, max_chars: 30, max_lines: 1 }
  for (const [key, value] of Object.entries(changes)) {
    assert.notEqual(presetKey(preset), presetKey({ ...preset, subtitle_style: { [key]: value } }), key)
  }
})

test('stored upload styles are validated before resume', () => {
  const sha256 = 'a'.repeat(64)
  const session = { id: 'a'.repeat(32), filename: 'clip.mp4', totalBytes: 100, lastModified: 1,
    fingerprint: `v2:clip.mp4:100:1:${sha256}`, sha256, chunkSize: 100, received: [0],
    preset, createdAt: '2026-10-06T00:00:00Z' }
  assert.equal(isUploadSession(session), true)
  assert.equal(isUploadSession({ ...session, preset: { ...preset, subtitle_style: { color: '#123456' } } }), true)
  for (const invalid of [null, [], { color: 'red' }, { font_family: 'Arial,99' }, { font_family: '  ' },
    { alignment: true }, { alignment: 10 }, { font_size: Infinity }, { margin_h: -1 },
    { max_lines: 0 }, { bold: 'false' }, { extra: true }]) {
    assert.equal(isUploadSession({ ...session, preset: { ...preset, subtitle_style: invalid } }), false)
  }
})
