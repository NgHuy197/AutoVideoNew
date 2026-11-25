import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'

const source = stripTypeScriptTypes(await readFile(new URL('../frontend/src/units.ts', import.meta.url), 'utf8'))
const units = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)

test('bytes use the largest whole unit with a Vietnamese decimal comma', () => {
  assert.equal(units.formatBytes(512), '512 B')
  assert.equal(units.formatBytes(1024), '1 KB')
  assert.equal(units.formatBytes(1536), '1,5 KB')
  assert.equal(units.formatBytes(1024 * 1024), '1 MB')
  assert.equal(units.formatBytes(1024 ** 3), '1 GB')
  assert.equal(units.formatBytes(2.5 * 1024 ** 3), '2,5 GB')
  assert.equal(units.formatBytes(350 * 1024 ** 2), '350 MB')
  assert.equal(units.formatBytes(1023.99 * 1024 ** 2), '1 GB')
})

test('durations keep the two largest parts', () => {
  assert.equal(units.formatDuration(45), '45 giây')
  assert.equal(units.formatDuration(125), '2 phút 5 giây')
  assert.equal(units.formatDuration(1800), '30 phút')
  assert.equal(units.formatDuration(5400), '1 giờ 30 phút')
  assert.equal(units.formatDuration(3661), '1 giờ 1 phút')
  assert.equal(units.formatDuration(18018), '5 giờ')
  assert.equal(units.formatDuration(0), '0 giây')
})

test('short times, clock positions and counts', () => {
  assert.equal(units.formatMs(850), '850 ms')
  assert.equal(units.formatMs(1234), '1,2 giây')
  assert.equal(units.formatClock(5300), '0:05')
  assert.equal(units.formatClock(65000), '1:05')
  assert.equal(units.formatClock(3725000), '1:02:05')
  assert.equal(units.formatCount(950), '950')
  assert.equal(units.formatCount(12300), '12 nghìn')
  assert.equal(units.formatCount(1500), '1,5 nghìn')
  assert.equal(units.formatCount(2500000), '2,5 triệu')
})

test('provider limit headers become readable', () => {
  assert.equal(units.formatResetDuration('6m0s'), '6 phút')
  assert.equal(units.formatResetDuration('1.5s'), '1,5 giây')
  assert.equal(units.formatResetDuration('20ms'), '20 ms')
  assert.equal(units.formatResetDuration('1732550400'), '1732550400')
  assert.equal(units.formatCountText('199000'), '199 nghìn')
  assert.equal(units.formatCountText('n/a'), 'n/a')
})
