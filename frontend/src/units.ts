// Human-readable units for the UI: "1,5 GB" instead of 1610612736 bytes,
// "1 giờ 30 phút" instead of 5400 seconds. Vietnamese decimal comma throughout.

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB']

function decimal(value: number, digits: number) {
  return new Intl.NumberFormat('vi-VN', { maximumFractionDigits: digits }).format(value)
}

/** 1024 → "1 KB", 1536 → "1,5 KB", 1073741824 → "1 GB". One decimal below 10, none above. */
export function formatBytes(bytes: number) {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  let value = bytes, unit = 0
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) { value /= 1024; unit++ }
  if (unit === 0) return `${Math.round(value)} B`
  // Rounding can reach 1024 (e.g. 1023,96 MB); show it as the next unit instead.
  if (Math.round(value) >= 1024 && unit < BYTE_UNITS.length - 1) { value /= 1024; unit++ }
  return `${decimal(value, value < 10 ? 1 : 0)} ${BYTE_UNITS[unit]}`
}

/** 45 → "45 giây", 125 → "2 phút 5 giây", 5400 → "1 giờ 30 phút", 18018 → "5 giờ".
 *  The largest unit plus the one right below it, so a skipped middle unit never misleads. */
export function formatDuration(seconds: number) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—'
  const total = Math.round(seconds)
  const values = [Math.floor(total / 86400), Math.floor(total % 86400 / 3600), Math.floor(total % 3600 / 60), total % 60]
  const names = ['ngày', 'giờ', 'phút', 'giây']
  const first = values.findIndex(value => value > 0)
  if (first < 0) return '0 giây'
  const parts = [`${values[first]} ${names[first]}`]
  if (first < 3 && values[first + 1]) parts.push(`${values[first + 1]} ${names[first + 1]}`)
  return parts.join(' ')
}

/** Latency and short waits: 850 → "850 ms", 1234 → "1,2 giây", 95000 → "1 phút 35 giây". */
export function formatMs(ms: number) {
  if (!Number.isFinite(ms) || ms < 0) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60000) return `${decimal(ms / 1000, 1)} giây`
  return formatDuration(ms / 1000)
}

/** Position in a video: 5300 → "0:05", 65000 → "1:05", 3725000 → "1:02:05". */
export function formatClock(ms: number) {
  const total = Math.max(0, Math.floor(ms / 1000))
  const hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), secs = total % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return hours ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`
}

/** Large counts such as tokens: 950 → "950", 12300 → "12,3 nghìn", 2500000 → "2,5 triệu". */
export function formatCount(count: number) {
  if (!Number.isFinite(count)) return '—'
  const abs = Math.abs(count)
  if (abs < 1000) return decimal(count, 0)
  if (abs < 1e6) return `${decimal(count / 1e3, abs < 1e4 ? 1 : 0)} nghìn`
  if (abs < 1e9) return `${decimal(count / 1e6, 1)} triệu`
  return `${decimal(count / 1e9, 1)} tỷ`
}

/** A provider's reset value such as "6m0s", "1.5s" or "20ms" → "6 phút"; anything else unchanged. */
export function formatResetDuration(value: string) {
  const parts = [...value.matchAll(/(\d+(?:\.\d+)?)(ms|h|m|s)/g)]
  if (!parts.length || parts.map(part => part[0]).join('') !== value.trim()) return value
  const scale: Record<string, number> = { h: 3600000, m: 60000, s: 1000, ms: 1 }
  return formatMs(parts.reduce((sum, [, amount, unit]) => sum + Number(amount) * scale[unit], 0))
}

/** A header value that may be a number: "199000" → "199 nghìn"; text stays as it is. */
export function formatCountText(value: string) {
  return /^\d+$/.test(value.trim()) ? formatCount(Number(value)) : value
}
