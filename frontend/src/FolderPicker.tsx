import { useEffect, useState } from 'react'
import { FolderPlus, RefreshCw } from 'lucide-react'
import { api, csrf } from './upload'

export type StorageFolder = { relative: string; path: string; name: string }

type Props = {
  scope: 'input' | 'output'
  label: string
  value: string
  onChange: (value: string) => void
  onError: (message: string) => void
  compact?: boolean
}

export function normalizeFolder(value?: string) {
  return String(value || '').replaceAll('\\', '/').replace(/^\/+|\/+$/g, '')
}

export default function FolderPicker({ scope, label, value, onChange, onError, compact = false }: Props) {
  const [folders, setFolders] = useState<StorageFolder[]>([])
  const [root, setRoot] = useState('')
  const [parent, setParent] = useState('')
  const [name, setName] = useState('')
  const [creating, setCreating] = useState(false)
  const load = async () => {
    try {
      const result = await api(`/storage-folders?scope=${scope}`)
      const rows = Array.isArray(result.folders) ? result.folders as StorageFolder[] : []
      setFolders(rows)
      setRoot(String(result.root || ''))
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Không thể tải danh sách thư mục')
    }
  }
  useEffect(() => {
    void load()
    const refresh = (event: Event) => {
      const changed = event as CustomEvent<{ scope?: string }>
      if (!changed.detail?.scope || changed.detail.scope === scope) void load()
    }
    window.addEventListener('videoauto:folders-changed', refresh)
    return () => window.removeEventListener('videoauto:folders-changed', refresh)
  }, [scope])
  const create = async () => {
    if (!name.trim()) return
    setCreating(true)
    try {
      const created = await api('/storage-folders', {
        method: 'POST', headers: { 'X-CSRF-Token': csrf() },
        body: JSON.stringify({ scope, parent, name: name.trim() }),
      }) as StorageFolder
      setName('')
      await load()
      onChange(created.relative)
      window.dispatchEvent(new CustomEvent('videoauto:folders-changed', { detail: { scope } }))
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Không thể tạo thư mục')
    } finally {
      setCreating(false)
    }
  }
  return <div className={`folder-picker${compact ? ' compact' : ''}`}>
    <label>{label}<select value={value} onChange={event => onChange(event.target.value)}>
      {value && !folders.some(folder => folder.relative === value) && <option value={value}>{value} (sẽ tạo lại)</option>}
      {folders.map(folder => <option key={folder.relative || '__root'} value={folder.relative}>{folder.relative || 'Thư mục gốc'}</option>)}
    </select></label>
    {!compact && <code title={root}>{root}{value ? `\\${value.replaceAll('/', '\\')}` : ''}</code>}
    <details className="folder-create"><summary><FolderPlus size={13}/> Tạo thư mục con</summary>
      <div className="folder-create-row">
        <select aria-label={`Thư mục cha ${label}`} value={parent} onChange={event => setParent(event.target.value)}>
          {folders.map(folder => <option key={folder.relative || '__root'} value={folder.relative}>{folder.relative || 'Thư mục gốc'}</option>)}
        </select>
        <input aria-label={`Tên thư mục mới ${label}`} value={name} maxLength={100} onChange={event => setName(event.target.value)} placeholder="Tên thư mục mới" />
        <button type="button" className="secondary" disabled={creating || !name.trim()} onClick={() => void create()}>{creating ? <RefreshCw className="spin" size={13}/> : <FolderPlus size={13}/>} Tạo</button>
      </div>
    </details>
  </div>
}
