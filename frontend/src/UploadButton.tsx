import { useEffect, useRef, useState } from 'react'
import { Check, CircleAlert, LoaderCircle, RotateCcw, UploadCloud, X } from 'lucide-react'
import {
  api,
  csrf,
  fingerprintFile,
  presetKey,
  readUploadSessions,
  receivedBytes,
  removeUploadSession,
  totalChunks,
  type UploadPreset,
  type UploadSession,
  upsertUploadSession,
} from './upload'

type UploadItemStatus = 'fingerprinting' | 'uploading' | 'completed' | 'error' | 'cancelled'

type UploadItem = {
  key: string
  file: File
  fingerprint: string
  sha256: string
  preset: UploadPreset
  session?: UploadSession
  status: UploadItemStatus
  phase?: 'hashing' | 'creating' | 'checking' | 'chunks' | 'completing'
  received: number[]
  hashProgress?: number
  error?: string
  note?: string
}

type UploadButtonProps = {
  onDone: () => void
  onError: (message: string) => void
  onClearError: () => void
  visible: boolean
  onShowQueue: () => void
}

type SavedPreset = { id: string; name: string; description?: string | null; config: UploadPreset }

const initialPreset: UploadPreset = {
  audio_mode: 'replace', subtitle_mode: 'burn', include_dubbing: true,
  include_original_track: false, priority: 0,
}

function presetLabel(preset: UploadPreset) {
  const audio = preset.audio_mode === 'mix' ? 'trộn audio gốc' : 'thay audio gốc'
  const subtitles = ({ burn: 'phụ đề ghi vào hình', soft: 'track phụ đề bật/tắt', srt: 'SRT rời', none: 'không phụ đề' } as Record<string, string>)[preset.subtitle_mode] || preset.subtitle_mode
  const secondary = preset.include_original_track ? ', giữ track audio gốc' : ''
  const priority = preset.priority ? `, ưu tiên ${preset.priority}` : ''
  return `${audio}, ${subtitles}${preset.include_dubbing ? ', có lồng tiếng' : ''}${secondary}${priority}`
}

function statusLabel(item: UploadItem) {
  if (item.status === 'fingerprinting') return item.note || 'Đang kiểm tra nhận diện tệp…'
  if (item.status === 'completed') return 'Đã đưa vào hàng đợi'
  if (item.status === 'cancelled') return 'Đã hủy tải lên'
  if (item.status === 'error') return 'Tải lên bị gián đoạn'
  const chunkSize = item.session?.chunkSize || 8 * 1024 * 1024
  const count = totalChunks(item.file.size, chunkSize)
  return `Đã nhận ${item.received.length}/${count} khối`
}

function itemProgress(item: UploadItem) {
  const session = item.session
  if (!session) return Math.round((item.hashProgress || 0) * 100)
  const bytes = receivedBytes(item.received, item.file.size, session.chunkSize)
  return Math.min(100, Math.round(bytes / item.file.size * 100))
}

export default function UploadButton({ onDone, onError, onClearError, visible, onShowQueue }: UploadButtonProps) {
  const input = useRef<HTMLInputElement>(null)
  const activeKeys = useRef(new Set<string>())
  const queuedKeys = useRef(new Set<string>())
  const pendingItems = useRef<UploadItem[]>([])
  const runningWorkers = useRef(0)
  const selectionCounter = useRef(0)
  const abortControllers = useRef(new Map<string, AbortController>())
  const hashControllers = useRef(new Map<string, AbortController>())
  const cancelledKeys = useRef(new Set<string>())
  // A completion abort is followed by a status reconciliation request. Keep
  // this marker until reconciliation settles so the original promise's
  // AbortError cannot overwrite a definitive COMPLETED result.
  const finalizationReconciliationKeys = useRef(new Set<string>())
  const [items, setItems] = useState<UploadItem[]>([])
  const [sessions, setSessions] = useState<UploadSession[]>(() => readUploadSessions())
  const [audioMode, setAudioMode] = useState(initialPreset.audio_mode)
  const [subtitleMode, setSubtitleMode] = useState(initialPreset.subtitle_mode)
  const [dub, setDub] = useState(initialPreset.include_dubbing)
  const [originalTrack, setOriginalTrack] = useState(Boolean(initialPreset.include_original_track))
  const [priority, setPriority] = useState(Number(initialPreset.priority) || 0)
  const [presets, setPresets] = useState<SavedPreset[]>([])
  const [selectedPresetId, setSelectedPresetId] = useState('')
  const [presetName, setPresetName] = useState('')
  const [presetBusy, setPresetBusy] = useState(false)

  const updateItem = (key: string, update: Partial<UploadItem>) => {
    setItems(previous => previous.map(item => item.key === key ? { ...item, ...update } : item))
  }

  const syncSessions = () => setSessions(readUploadSessions())

  const currentPreset = (): UploadPreset => ({
    audio_mode: audioMode,
    subtitle_mode: subtitleMode,
    include_dubbing: dub,
    include_original_track: originalTrack,
    priority: Number.isSafeInteger(priority) ? priority : 0,
  })

  const applyPreset = (preset: UploadPreset) => {
    setAudioMode(preset.audio_mode === 'mix' ? 'mix' : 'replace')
    setSubtitleMode(['burn', 'soft', 'srt', 'none'].includes(preset.subtitle_mode) ? preset.subtitle_mode : 'burn')
    setDub(Boolean(preset.include_dubbing))
    setOriginalTrack(Boolean(preset.include_original_track))
    setPriority(Number.isSafeInteger(preset.priority) ? Number(preset.priority) : 0)
  }

  const loadPresets = async () => {
    try {
      const rows = await api('/presets')
      setPresets(Array.isArray(rows) ? rows as SavedPreset[] : [])
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Không thể tải preset')
    }
  }

  const selectPreset = (id: string) => {
    setSelectedPresetId(id)
    const selected = presets.find(preset => preset.id === id)
    if (!selected) return
    setPresetName(selected.name)
    applyPreset(selected.config)
  }

  const savePreset = async (updateExisting: boolean) => {
    const name = presetName.trim()
    if (!name) {
      onError('Nhập tên preset trước khi lưu.')
      return
    }
    setPresetBusy(true)
    try {
      const method = updateExisting && selectedPresetId ? 'PATCH' : 'POST'
      const path = updateExisting && selectedPresetId ? `/presets/${selectedPresetId}` : '/presets'
      const saved = await api(path, {
        method,
        headers: { 'X-CSRF-Token': csrf() },
        body: JSON.stringify({ name, config: currentPreset() }),
      }) as SavedPreset
      await loadPresets()
      setSelectedPresetId(saved.id)
      setPresetName(saved.name)
      onClearError()
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Không thể lưu preset')
    } finally {
      setPresetBusy(false)
    }
  }

  const deletePreset = async () => {
    const selected = presets.find(preset => preset.id === selectedPresetId)
    if (!selected) return
    if (!window.confirm(`Xóa preset “${selected.name}”?`)) return
    setPresetBusy(true)
    try {
      await api(`/presets/${selected.id}`, { method: 'DELETE', headers: { 'X-CSRF-Token': csrf() } })
      setSelectedPresetId('')
      setPresetName('')
      await loadPresets()
      onClearError()
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Không thể xóa preset')
    } finally {
      setPresetBusy(false)
    }
  }

  const uploadItem = async (item: UploadItem) => {
    if (activeKeys.current.has(item.key) || cancelledKeys.current.has(item.key)) return
    activeKeys.current.add(item.key)
    const controller = new AbortController()
    abortControllers.current.set(item.key, controller)
    try {
      let session = item.session
      if (!session) {
        updateItem(item.key, { status: 'uploading', phase: 'creating', note: 'Đang tạo phiên upload…' })
        const created = await api('/uploads', {
          method: 'POST',
          headers: { 'X-CSRF-Token': csrf() },
          body: JSON.stringify({ filename: item.file.name, total_bytes: item.file.size, sha256: item.sha256 }),
          signal: controller.signal,
        })
        session = {
          id: String(created.id),
          filename: item.file.name,
          totalBytes: item.file.size,
          lastModified: item.file.lastModified,
          fingerprint: item.fingerprint,
          sha256: item.sha256,
          chunkSize: Number(created.chunk_size) || 8 * 1024 * 1024,
          received: Array.isArray(created.received) ? created.received.map(Number) : [],
          preset: item.preset,
          createdAt: new Date().toISOString(),
        }
        item.session = session
        upsertUploadSession(session)
        syncSessions()
      }

      // Always ask the server for its acknowledged set. This is what makes a
      // retry safe after a browser or network failure: acknowledged chunks are
      // skipped, even if the local session was written just before a crash.
      updateItem(item.key, { status: 'uploading', phase: 'checking', note: 'Đang kiểm tra khối đã nhận…' })
      const status = await api(`/uploads/${session.id}`, { signal: controller.signal })
      if (status.filename !== session.filename || Number(status.total_bytes) !== session.totalBytes) {
        throw new Error('Thông tin phiên upload không khớp với tệp đã chọn; không tiếp tục để tránh trộn nội dung.')
      }
      if (status.status === 'COMPLETED') {
        const result = await api(`/uploads/${session.id}/complete`, {
          method: 'POST',
          headers: { 'X-CSRF-Token': csrf() },
          body: JSON.stringify({ preset: session.preset, sha256: session.sha256 }),
          signal: controller.signal,
        })
        if (cancelledKeys.current.has(item.key) && item.phase === 'completing') return
        removeUploadSession(session.id)
        syncSessions()
        updateItem(item.key, { session, received: session.received, status: 'completed', phase: undefined, error: undefined, note: result.idempotent ? 'Phiên upload đã hoàn tất trước đó; đã khôi phục job.' : undefined })
        onClearError()
        onDone()
        return
      }
      const serverChunkSize = Number(status.chunk_size) || session.chunkSize
      const serverReceived = Array.isArray(status.received)
        ? status.received.map(Number).filter((index: number) => Number.isInteger(index) && index >= 0 && index < totalChunks(session!.totalBytes, serverChunkSize))
        : session.received
      session = { ...session, chunkSize: serverChunkSize, received: serverReceived }
      item.session = session
      upsertUploadSession(session)
      syncSessions()
      updateItem(item.key, { session, received: session.received, status: 'uploading', phase: 'chunks', error: undefined })

      const count = totalChunks(item.file.size, session.chunkSize)
      const acknowledged = new Set(session.received)
      for (let index = 0; index < count; index += 1) {
        if (acknowledged.has(index)) continue
        const start = index * session.chunkSize
        const data = await item.file.slice(start, Math.min(item.file.size, start + session.chunkSize)).arrayBuffer()
        const response = await api(`/uploads/${session.id}/chunks/${index}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/octet-stream', 'X-CSRF-Token': csrf() },
          body: data,
          signal: controller.signal,
        })
        const received = Array.isArray(response.received) ? response.received.map(Number) : [...acknowledged, index]
        acknowledged.clear()
        received.forEach((receivedIndex: number) => acknowledged.add(receivedIndex))
        session = { ...session, received: [...acknowledged].sort((a, b) => a - b) }
        item.session = session
        upsertUploadSession(session)
        syncSessions()
        updateItem(item.key, { session, received: session.received, status: 'uploading', error: undefined })
      }

      updateItem(item.key, { status: 'uploading', phase: 'completing', note: 'Đang hoàn tất upload trên máy chủ…' })
      const result = await api(`/uploads/${session.id}/complete`, {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf() },
        body: JSON.stringify({ preset: session.preset, sha256: session.sha256 }),
        signal: controller.signal,
      })
      if (cancelledKeys.current.has(item.key) && item.phase === 'completing') return
      removeUploadSession(session.id)
      syncSessions()
      updateItem(item.key, { session, received: session.received, status: 'completed', phase: undefined, error: undefined, note: result.idempotent ? 'Phiên upload đã hoàn tất trước đó; đã khôi phục job.' : undefined })
      onClearError()
      onDone()
    } catch (error) {
      if (finalizationReconciliationKeys.current.has(item.key)) {
        syncSessions()
        return
      }
      // The reconciliation may have completed and cleared its marker before
      // this aborted promise schedules its catch handler. The stale item still
      // identifies a completion request, so leave the reconciler's terminal
      // UI state untouched.
      if (cancelledKeys.current.has(item.key) && item.phase === 'completing') {
        syncSessions()
        return
      }
      if (cancelledKeys.current.has(item.key) || (error instanceof DOMException && error.name === 'AbortError')) {
        updateItem(item.key, { status: 'cancelled', phase: undefined, error: undefined, note: 'Đã hủy. Phiên máy chủ vẫn được lưu; chọn Thử lại để tiếp tục.' })
        syncSessions()
        return
      }
      const message = error instanceof Error ? error.message : 'Không thể tải video lên'
      updateItem(item.key, { status: 'error', error: message })
      onError(`${item.file.name}: ${message}. Bạn có thể chọn lại đúng tệp để tiếp tục.`)
    } finally {
      activeKeys.current.delete(item.key)
      abortControllers.current.delete(item.key)
    }
  }

  // Keep one queue for the whole component lifetime, so choosing more files
  // while an earlier selection is running cannot create a third worker.
  const drainQueue = () => {
    while (runningWorkers.current < 2 && pendingItems.current.length > 0) {
      const item = pendingItems.current.shift() as UploadItem
      queuedKeys.current.delete(item.key)
      runningWorkers.current += 1
      void uploadItem(item).finally(() => {
        runningWorkers.current -= 1
        drainQueue()
      })
    }
  }

  const enqueue = (nextItems: UploadItem[]) => {
    for (const item of nextItems) {
      if (activeKeys.current.has(item.key) || queuedKeys.current.has(item.key) || cancelledKeys.current.has(item.key)) continue
      queuedKeys.current.add(item.key)
      pendingItems.current.push(item)
    }
    drainQueue()
  }

  // A canceled finalize request may have reached the server before the
  // browser aborted its response. Reconcile with a fresh request before
  // claiming that the upload was canceled.
  const reconcileFinalization = async (item: UploadItem) => {
    const session = item.session
    if (!session) {
      updateItem(item.key, { status: 'cancelled', phase: undefined, note: 'Đã hủy trước khi tạo phiên máy chủ.' })
      return
    }
    const reconcileController = new AbortController()
    try {
      for (let attempt = 0; attempt < 5; attempt += 1) {
        try {
        const status = await api(`/uploads/${session.id}`, { signal: reconcileController.signal })
        if (status.status === 'COMPLETED') {
          const result = await api(`/uploads/${session.id}/complete`, {
            method: 'POST',
            headers: { 'X-CSRF-Token': csrf() },
            body: JSON.stringify({ preset: session.preset, sha256: session.sha256 }),
            signal: reconcileController.signal,
          })
          removeUploadSession(session.id)
          syncSessions()
          updateItem(item.key, { session, status: 'completed', phase: undefined, error: undefined, note: result.idempotent ? 'Upload đã hoàn tất trước khi hủy; đã khôi phục job.' : 'Upload đã hoàn tất trên máy chủ.' })
          onClearError()
          onDone()
          return
        }
        if (status.status === 'UPLOADING') {
          const settled = { ...session, finalizationPending: false }
          upsertUploadSession(settled)
          syncSessions()
          updateItem(item.key, { session: settled, status: 'cancelled', phase: undefined, error: undefined, note: 'Đã hủy kết nối upload. Phiên máy chủ được lưu để tiếp tục khi cần.' })
          return
        }
        // COMPLETING means the server still owns finalization. Keep the
        // durable session and wait briefly for a definitive status.
        if (attempt < 4) await new Promise(resolve => setTimeout(resolve, 500))
        } catch (error) {
          const message = error instanceof Error ? error.message : 'Không thể xác minh trạng thái upload'
          updateItem(item.key, { status: 'error', phase: undefined, error: message, note: 'Chưa thể xác minh việc hoàn tất; phiên upload vẫn được lưu. Thử lại sau.' })
          onError(`${item.file.name}: ${message}`)
          return
        }
      }
      updateItem(item.key, { status: 'error', phase: undefined, error: 'Máy chủ vẫn đang hoàn tất upload.', note: 'Chưa thể xác minh việc hủy; phiên upload vẫn được lưu. Thử lại sau.' })
      onError(`${item.file.name}: máy chủ vẫn đang hoàn tất upload; phiên được giữ để xác minh lại.`)
    } finally {
      finalizationReconciliationKeys.current.delete(item.key)
    }
  }

  const cancelItem = (item: UploadItem) => {
    if (item.status === 'completed') return
    if (item.phase === 'completing' && item.session) {
      if (finalizationReconciliationKeys.current.has(item.key)) return
      cancelledKeys.current.add(item.key)
      finalizationReconciliationKeys.current.add(item.key)
      const pendingSession = { ...item.session, finalizationPending: true }
      upsertUploadSession(pendingSession)
      syncSessions()
      abortControllers.current.get(item.key)?.abort()
      updateItem(item.key, { session: pendingSession, status: 'fingerprinting', phase: 'completing', error: undefined, note: 'Đang xác minh máy chủ trước khi báo hủy…' })
      void reconcileFinalization({ ...item, session: pendingSession })
      return
    }
    cancelledKeys.current.add(item.key)
    hashControllers.current.get(item.key)?.abort()
    abortControllers.current.get(item.key)?.abort()
    pendingItems.current = pendingItems.current.filter(pending => pending.key !== item.key)
    queuedKeys.current.delete(item.key)
    updateItem(item.key, { status: 'cancelled', error: undefined, note: item.session ? 'Đã hủy. Phiên máy chủ vẫn được lưu; chọn Thử lại để tiếp tục.' : 'Đã hủy trước khi tạo phiên máy chủ.' })
  }

  const handleFiles = async (fileList: FileList | null) => {
    if (!fileList?.length) return
    onClearError()
    const preset = currentPreset()
    const selectedFiles = Array.from(fileList)
    const prepared: UploadItem[] = []
    const selectionId = ++selectionCounter.current
    const hashingItems = selectedFiles.map((file, index): UploadItem => ({
      key: `hashing:${selectionId}:${index}:${file.name}:${file.size}:${file.lastModified}`,
      file,
      fingerprint: '',
      sha256: '',
      preset,
      status: 'fingerprinting',
      received: [],
      hashProgress: 0,
    }))
    setItems(previous => [...previous, ...hashingItems])
    for (const hashingItem of hashingItems) {
      if (cancelledKeys.current.has(hashingItem.key)) continue
      const hashController = new AbortController()
      hashControllers.current.set(hashingItem.key, hashController)
      let identity: Awaited<ReturnType<typeof fingerprintFile>>
      try {
        identity = await fingerprintFile(hashingItem.file, progress => updateItem(hashingItem.key, { hashProgress: progress, note: `Đang băm toàn bộ tệp… ${Math.round(progress * 100)}%` }), hashController.signal)
      } catch (error) {
        if (hashController.signal.aborted || cancelledKeys.current.has(hashingItem.key)) {
          updateItem(hashingItem.key, { status: 'cancelled', note: 'Đã hủy khi kiểm tra nội dung tệp.' })
        } else {
          updateItem(hashingItem.key, { status: 'error', error: error instanceof Error ? error.message : 'Không thể kiểm tra tệp' })
        }
        continue
      } finally {
        hashControllers.current.delete(hashingItem.key)
      }
      const sameFile = readUploadSessions().find(session => session.fingerprint === identity.fingerprint && session.sha256 === identity.sha256)
      const matchingSession = sameFile && presetKey(sameFile.preset) === presetKey(preset) ? sameFile : undefined
      const item: UploadItem = {
        ...hashingItem,
        key: `${identity.fingerprint}:${presetKey(preset)}`,
        fingerprint: identity.fingerprint,
        sha256: identity.sha256,
        session: matchingSession,
        received: matchingSession?.received || [],
        hashProgress: 1,
        note: sameFile && !matchingSession ? `Đã tìm phiên dở với preset “${presetLabel(sameFile.preset)}”. Đang tạo phiên mới cho preset hiện tại.` : matchingSession ? 'Có phiên dở cùng nhận diện tệp và preset; sẽ tiếp tục các khối còn thiếu.' : undefined,
      }
      // A fresh selection of the same file replaces its old visual row, while
      // the persisted server session remains the source of truth for resume.
      cancelledKeys.current.delete(item.key)
      setItems(previous => [...previous.filter(previousItem => previousItem.key !== item.key), item].filter(previousItem => previousItem.key !== hashingItem.key))
      prepared.push(item)
    }
    enqueue(prepared)
    if (input.current) input.current.value = ''
  }

  const retry = (item: UploadItem) => {
    if (activeKeys.current.has(item.key) || item.status === 'completed') return
    onClearError()
    cancelledKeys.current.delete(item.key)
    if (!item.sha256) {
      input.current?.click()
      return
    }
    void (async () => {
      const identity = await fingerprintFile(item.file)
      if (identity.fingerprint !== item.fingerprint || identity.sha256 !== item.sha256) {
        updateItem(item.key, { status: 'error', error: 'Nội dung tệp đã thay đổi; không tiếp tục phiên upload cũ.' })
        onError(`${item.file.name}: nội dung tệp đã thay đổi, phiên upload cũ được giữ nguyên.`)
        return
      }
      updateItem(item.key, { status: 'fingerprinting', error: undefined })
      enqueue([item])
    })()
  }

  useEffect(() => { void loadPresets() }, [])

  useEffect(() => {
    const onStorage = () => syncSessions()
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener('storage', onStorage)
      abortControllers.current.forEach(controller => controller.abort())
      hashControllers.current.forEach(controller => controller.abort())
      pendingItems.current = []
      queuedKeys.current.clear()
    }
  }, [])

  const backgroundItems = items.filter(item => item.status !== 'completed' && item.status !== 'error' && item.status !== 'cancelled')
  if (!visible) return <div className="upload-background-status" role="status">
    <span>{backgroundItems.length > 0 ? `${backgroundItems.length} video đang tải lên` : sessions.length > 0 ? `${sessions.length} phiên upload được lưu` : 'Upload sẵn sàng'}</span>
    <button onClick={onShowQueue}>Mở hàng đợi</button>
    {backgroundItems.map(item => <button className="background-cancel" key={item.key} onClick={() => cancelItem(item)} aria-label={`Hủy tải ${item.file.name}`}>Hủy {item.file.name}</button>)}
  </div>

  return <div className="upload-area">
    <div className="upload-controls">
      <input ref={input} type="file" accept="video/*,.mkv" multiple hidden onChange={event => void handleFiles(event.target.files)} />
      <label>Preset<select value={selectedPresetId} onChange={event => selectPreset(event.target.value)}><option value="">Tùy chỉnh hiện tại</option>{presets.map(preset => <option value={preset.id} key={preset.id}>{preset.name}</option>)}</select></label>
      <label>Audio<select value={audioMode} onChange={event => setAudioMode(event.target.value)}><option value="replace">Thay audio gốc</option><option value="mix">Trộn audio gốc −18 dB</option></select></label>
      <label>Phụ đề<select value={subtitleMode} onChange={event => setSubtitleMode(event.target.value)}><option value="burn">Ghi vào hình</option><option value="soft">Track bật/tắt</option><option value="srt">SRT rời</option><option value="none">Không phụ đề</option></select></label>
      <label className="check-label"><input type="checkbox" checked={dub} onChange={event => setDub(event.target.checked)} /> Lồng tiếng</label>
      <label className="check-label"><input type="checkbox" checked={originalTrack} onChange={event => setOriginalTrack(event.target.checked)} /> Giữ track gốc phụ</label>
      <label>Ưu tiên<input className="priority-input" type="number" min="-100" max="100" step="1" value={priority} onChange={event => setPriority(Number.parseInt(event.target.value, 10) || 0)} /></label>
      <button className="primary upload" onClick={() => input.current?.click()}><UploadCloud size={18} /> Thêm video</button>
    </div>
    <div className="preset-controls" aria-label="Quản lý preset">
      <input value={presetName} maxLength={160} onChange={event => setPresetName(event.target.value)} placeholder="Tên preset" aria-label="Tên preset" />
      <button className="secondary" disabled={presetBusy} onClick={() => void savePreset(false)}>Lưu mới</button>
      <button className="secondary" disabled={presetBusy || !selectedPresetId} onClick={() => void savePreset(true)}>Cập nhật</button>
      <button className="text-danger" disabled={presetBusy || !selectedPresetId} onClick={() => void deletePreset()}>Xóa preset</button>
    </div>
    <p className="upload-help">Tối đa 2 video tải song song. Nếu trình duyệt bị reload, hãy chọn lại đúng tệp; ứng dụng chỉ tiếp tục khi nhận diện nội dung và preset trùng khớp. Tiến độ tính theo khối máy chủ đã xác nhận.</p>
    {sessions.length > 0 && <div className="resume-hint" role="status"><RotateCcw size={14} /><span><strong>{sessions.length} phiên upload dở được lưu trên máy.</strong> Đặt đúng preset bên trên rồi chọn lại tệp để tiếp tục.</span></div>}
    {items.length > 0 && <div className="upload-items" aria-live="polite" aria-label="Tiến độ tải video">
      {items.map(item => {
        const progress = itemProgress(item)
        return <div className="upload-item" key={item.key}>
          <div className="upload-item-head"><div className="upload-item-name" title={item.file.name}>{item.file.name}</div><span className={`upload-item-status ${item.status}`}>{item.status === 'uploading' && <LoaderCircle size={13} className="spin" />}{item.status === 'completed' && <Check size={13} />}{item.status === 'error' && <CircleAlert size={13} />}{statusLabel(item)}</span></div>
          <div className="upload-progress" role="progressbar" aria-label={`Tiến độ ${item.file.name}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><i style={{ width: `${progress}%` }} /></div>
          {item.status === 'uploading' && <div className="upload-percent">{progress}% · khối đã xác nhận, không tính thời gian chờ</div>}
          {item.note && <div className="upload-note">{item.note}</div>}
          {item.error && <div className="upload-error"><CircleAlert size={13} />{item.error}<button className="upload-retry" onClick={() => retry(item)}><RotateCcw size={13} /> Thử lại</button></div>}
          {item.status !== 'completed' && item.status !== 'error' && item.status !== 'cancelled' && <button className="upload-cancel" onClick={() => cancelItem(item)}><X size={13} /> Hủy tải lên</button>}
          {item.status === 'cancelled' && <button className="upload-retry standalone" onClick={() => retry(item)}><RotateCcw size={13} /> {item.sha256 ? 'Thử lại' : 'Chọn lại tệp'}</button>}
        </div>
      })}
      <button className="upload-clear" onClick={() => setItems([])}><X size={13} /> Xóa danh sách hiển thị</button>
    </div>}
  </div>
}
