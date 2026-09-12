import { sha256 } from '@noble/hashes/sha256'

export const API = '/api/v1'
export const UPLOAD_SESSIONS_KEY = 'videoauto.upload-sessions.v1'
export const FINGERPRINT_CHUNK_BYTES = 8 * 1024 * 1024

export type UploadPreset = {
  audio_mode: string
  subtitle_mode: string
  include_dubbing: boolean
  include_original_track?: boolean
  priority?: number
}

export type UploadSession = {
  id: string
  filename: string
  totalBytes: number
  lastModified: number
  fingerprint: string
  sha256: string
  chunkSize: number
  received: number[]
  preset: UploadPreset
  createdAt: string
  finalizationPending?: boolean
}

export function csrf() {
  return document.cookie.split('; ').find(value => value.startsWith('videoauto_csrf='))?.split('=')[1] || ''
}

export async function api(path: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers || {})
  const bodyIsBinary = options.body instanceof ArrayBuffer || options.body instanceof Blob || options.body instanceof Uint8Array
  if (!headers.has('Content-Type') && !bodyIsBinary) headers.set('Content-Type', 'application/json')
  const response = await fetch(API + path, { ...options, headers })
  const text = await response.text()
  let payload: any = {}
  if (text) {
    try { payload = JSON.parse(text) } catch { payload = { detail: text } }
  }
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`)
  return payload
}

/**
 * Creates a stable identity and the server's expected full SHA-256 digest.
 * Hashing is incremental: at most one 8 MiB slice is held in memory, and an
 * event-loop yield after each slice keeps the local UI responsive for large
 * videos. The caller can use progress to show that hashing is real work.
 */
export async function fingerprintFile(file: File, onProgress?: (progress: number) => void, signal?: AbortSignal) {
  const hasher = sha256.create()
  const count = Math.max(1, Math.ceil(file.size / FINGERPRINT_CHUNK_BYTES))
  for (let index = 0; index < count; index += 1) {
    if (signal?.aborted) throw new DOMException('Upload cancelled', 'AbortError')
    const start = index * FINGERPRINT_CHUNK_BYTES
    const bytes = new Uint8Array(await file.slice(start, Math.min(file.size, start + FINGERPRINT_CHUNK_BYTES)).arrayBuffer())
    hasher.update(bytes)
    onProgress?.((index + 1) / count)
    // Let React paint the hashing status and let a cancel/reload event run.
    await new Promise<void>(resolve => setTimeout(resolve, 0))
  }
  if (signal?.aborted) throw new DOMException('Upload cancelled', 'AbortError')
  const digest = Array.from(hasher.digest(), byte => byte.toString(16).padStart(2, '0')).join('')
  return { fingerprint: `v2:${file.name}:${file.size}:${file.lastModified}:${digest}`, sha256: digest }
}

export function presetKey(preset: UploadPreset) {
  return JSON.stringify({
    audio_mode: preset.audio_mode,
    subtitle_mode: preset.subtitle_mode,
    include_dubbing: Boolean(preset.include_dubbing),
    include_original_track: Boolean(preset.include_original_track),
    priority: Number.isSafeInteger(preset.priority) ? preset.priority : 0,
  })
}

const uploadIdPattern = /^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/i
const sha256Pattern = /^[0-9a-f]{64}$/i
const knownAudioModes = new Set(['replace', 'mix'])
const knownSubtitleModes = new Set(['burn', 'soft', 'srt', 'none'])

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Validate localStorage data before it can influence a resume request. */
export function isUploadSession(value: unknown): value is UploadSession {
  if (!isPlainRecord(value)) return false
  const id = value.id
  const filename = value.filename
  const totalBytes = value.totalBytes
  const lastModified = value.lastModified
  const fingerprint = value.fingerprint
  const sha256 = value.sha256
  const chunkSize = value.chunkSize
  const received = value.received
  const preset = value.preset
  const createdAt = value.createdAt
  if (typeof id !== 'string' || !uploadIdPattern.test(id)) return false
  if (typeof filename !== 'string' || filename.length === 0 || filename.length > 512) return false
  if (typeof totalBytes !== 'number' || !Number.isSafeInteger(totalBytes) || totalBytes <= 0) return false
  if (typeof lastModified !== 'number' || !Number.isFinite(lastModified) || lastModified < 0) return false
  if (typeof sha256 !== 'string' || !sha256Pattern.test(sha256)) return false
  if (typeof fingerprint !== 'string' || fingerprint !== `v2:${filename}:${totalBytes}:${lastModified}:${sha256}`) return false
  if (typeof chunkSize !== 'number' || !Number.isSafeInteger(chunkSize) || chunkSize <= 0) return false
  if (!Array.isArray(received)) return false
  const count = totalChunks(totalBytes, chunkSize)
  const indexes = received as unknown[]
  if (indexes.some(index => typeof index !== 'number' || !Number.isSafeInteger(index) || index < 0 || index >= count)) return false
  if (new Set(indexes).size !== indexes.length) return false
  if (!isPlainRecord(preset) || !knownAudioModes.has(String(preset.audio_mode)) ||
      !knownSubtitleModes.has(String(preset.subtitle_mode)) || typeof preset.include_dubbing !== 'boolean') return false
  for (const key of ['include_original_track', 'include_original_audio', 'include_original_audio_track', 'original_audio_secondary']) {
    if (key in preset && typeof preset[key] !== 'boolean') return false
  }
  if ('priority' in preset && (!Number.isSafeInteger(preset.priority) || typeof preset.priority !== 'number')) return false
  if (typeof createdAt !== 'string' || Number.isNaN(Date.parse(createdAt))) return false
  if ('finalizationPending' in value && typeof value.finalizationPending !== 'boolean') return false
  return true
}

export function readUploadSessions(): UploadSession[] {
  if (typeof window === 'undefined') return []
  try {
    const value = JSON.parse(window.localStorage.getItem(UPLOAD_SESSIONS_KEY) || '[]')
    if (!Array.isArray(value)) return []
    const valid = value.filter(isUploadSession)
    // Remove malformed/stale entries immediately so a future tab cannot pick
    // them up. Storage may be read-only in privacy mode, hence the guarded
    // write and the valid return value even when setItem is unavailable.
    if (valid.length !== value.length) {
      try { window.localStorage.setItem(UPLOAD_SESSIONS_KEY, JSON.stringify(valid)) } catch { /* storage can be disabled */ }
    }
    return valid
  } catch {
    return []
  }
}

export function writeUploadSessions(sessions: UploadSession[]) {
  if (typeof window === 'undefined') return
  try { window.localStorage.setItem(UPLOAD_SESSIONS_KEY, JSON.stringify(sessions)) } catch { /* storage can be disabled */ }
}

export function upsertUploadSession(session: UploadSession) {
  const sessions = readUploadSessions().filter(item => item.id !== session.id)
  sessions.push(session)
  writeUploadSessions(sessions)
  return sessions
}

export function removeUploadSession(id: string) {
  const sessions = readUploadSessions().filter(item => item.id !== id)
  writeUploadSessions(sessions)
  return sessions
}

export function receivedBytes(received: Iterable<number>, totalBytes: number, chunkSize: number) {
  let bytes = 0
  for (const index of received) {
    const start = index * chunkSize
    if (start >= 0 && start < totalBytes) bytes += Math.min(chunkSize, totalBytes - start)
  }
  return bytes
}

export function totalChunks(totalBytes: number, chunkSize: number) {
  return Math.max(1, Math.ceil(totalBytes / chunkSize))
}

/** Shared bounded worker primitive used by upload tests and queue code. */
export async function runWithConcurrency<T, R>(items: T[], limit: number, worker: (item: T, index: number) => Promise<R>) {
  if (limit < 1) throw new Error('concurrency limit must be positive')
  const results = new Array<R>(items.length)
  let nextIndex = 0
  const run = async () => {
    while (true) {
      const index = nextIndex++
      if (index >= items.length) return
      results[index] = await worker(items[index], index)
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, run))
  return results
}
