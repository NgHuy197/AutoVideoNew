import { useEffect, useId, useState, type CSSProperties } from 'react'
import { DEFAULT_SUBTITLE_STYLE, SUBTITLE_POSITIONS, type SubtitleStyle } from './subtitleStyle'

type Props = { value: SubtitleStyle; onChange: (value: SubtitleStyle) => void; mode: string }

function ColorField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])
  return <div className="subtitle-color-field"><span>{label}</span><div className="subtitle-color-inputs">
    <input type="color" aria-label={label} value={value} onChange={event => onChange(event.target.value.toUpperCase())} />
    <input type="text" aria-label={`Mã ${label.toLowerCase()}`} value={draft} maxLength={7} placeholder="#FFFFFF" onChange={event => {
      const next = event.target.value.toUpperCase()
      setDraft(next)
      if (/^#[0-9A-F]{6}$/.test(next)) onChange(next)
    }} onBlur={() => setDraft(value)} />
  </div></div>
}

export default function SubtitleStyleEditor({ value, onChange, mode }: Props) {
  const fontListId = useId()
  const [portrait, setPortrait] = useState(false)
  const [sample, setSample] = useState('Xin chào! Đây là phụ đề tiếng Việt của bạn.')
  const change = <K extends keyof SubtitleStyle>(key: K, next: SubtitleStyle[K]) => onChange({ ...value, [key]: next })
  const column = (value.alignment - 1) % 3
  const row = value.alignment > 6 ? 'flex-start' : value.alignment > 3 ? 'center' : 'flex-end'
  const aspect = portrait ? 9 / 16 : 16 / 9
  const words = sample.split(/\s+/).filter(Boolean)
  const lines: string[] = []
  let line = ''
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word
    if (line && candidate.length > value.max_chars) { lines.push(line); line = word } else line = candidate
  }
  if (line) lines.push(line)
  const previewStyle: CSSProperties = {
    justifyContent: row,
    alignItems: ['flex-start', 'center', 'flex-end'][column],
    padding: `${value.margin_v / aspect}cqw ${value.margin_h}cqw`,
    textAlign: (['left', 'center', 'right'] as const)[column],
  }
  const textStyle: CSSProperties = {
    color: value.color, fontFamily: `"${value.font_family.replaceAll('"', '')}", sans-serif`,
    fontSize: `${value.font_size / aspect}cqw`, lineHeight: 1.2,
    fontWeight: value.bold ? 700 : 400, fontStyle: value.italic ? 'italic' : 'normal',
    WebkitTextStroke: `${value.outline_width / (10.8 * aspect)}cqw ${value.outline_color}`,
    paintOrder: 'stroke fill',
    textShadow: value.shadow ? `${value.shadow / (10.8 * aspect)}cqw ${value.shadow / (10.8 * aspect)}cqw 0 #0008` : 'none',
  }
  const numeric = (key: 'outline_width' | 'shadow' | 'margin_v' | 'margin_h' | 'max_chars' | 'max_lines', label: string, min: number, max: number, step: number) =>
    <label>{label}<input type="number" min={min} max={max} step={step} value={value[key]} onChange={event => {
      const number = event.currentTarget.valueAsNumber
      if (Number.isFinite(number)) change(key, Math.min(max, Math.max(min, step === 1 ? Math.round(number) : number)))
    }} /></label>

  return <details className="subtitle-customizer">
    <summary>Tùy chỉnh phụ đề <span>{value.font_family} · {value.font_size}% · {SUBTITLE_POSITIONS.find(position => position.value === value.alignment)?.label}</span></summary>
    <div className="subtitle-customizer-body">
      <p className="subtitle-style-note">{mode === 'burn' ? 'Áp dụng cho các video thêm sau khi chỉnh. Lưu preset để dùng lại kiểu này.' : mode === 'none' ? 'Chọn “Ghi vào hình” để xuất video với kiểu phụ đề này.' : 'SRT và track bật/tắt trong MP4 không giữ đầy đủ kiểu dáng; trình phát quyết định cách hiển thị. Chọn “Ghi vào hình” để áp dụng các tùy chỉnh.'}</p>
      <div className="subtitle-style-layout">
        <fieldset disabled={mode !== 'burn'} className="subtitle-style-fields">
          <legend>Kiểu chữ và bố cục</legend>
          <label className="subtitle-font">Font chữ<input list={fontListId} maxLength={80} value={value.font_family} onChange={event => {
            if (!/[,\\{}\x00-\x1f\x7f]/.test(event.target.value)) change('font_family', event.target.value)
          }} onBlur={() => change('font_family', value.font_family.trim() || DEFAULT_SUBTITLE_STYLE.font_family)} />
            <datalist id={fontListId}>{['Noto Sans', 'Arial', 'Segoe UI', 'Tahoma', 'Verdana', 'Times New Roman', 'Georgia', 'Calibri'].map(font => <option key={font} value={font} />)}</datalist>
          </label>
          <label>Cỡ chữ: {value.font_size}% chiều cao<input type="range" min="1" max="15" step="0.1" value={value.font_size} onChange={event => change('font_size', Number(event.target.value))} /></label>
          <ColorField label="Màu chữ" value={value.color} onChange={color => change('color', color)} />
          <ColorField label="Màu viền" value={value.outline_color} onChange={color => change('outline_color', color)} />
          <label className="subtitle-position">Vị trí<select value={value.alignment} onChange={event => change('alignment', Number(event.target.value))}>{SUBTITLE_POSITIONS.map(position => <option key={position.value} value={position.value}>{position.label}</option>)}</select></label>
          {numeric('outline_width', 'Độ dày viền', 0, 8, 0.5)}
          {numeric('shadow', 'Độ bóng', 0, 8, 0.5)}
          {numeric('margin_v', 'Cách mép trên/dưới (%)', 0, 30, 0.5)}
          {numeric('margin_h', 'Cách mép trái/phải (%)', 0, 30, 0.5)}
          {numeric('max_chars', 'Ký tự mỗi dòng', 10, 100, 1)}
          {numeric('max_lines', 'Số dòng mỗi lần hiện', 1, 4, 1)}
          <label className="check-label"><input type="checkbox" checked={value.bold} onChange={event => change('bold', event.target.checked)} /> Chữ đậm</label>
          <label className="check-label"><input type="checkbox" checked={value.italic} onChange={event => change('italic', event.target.checked)} /> Chữ nghiêng</label>
          <button type="button" className="secondary subtitle-reset" onClick={() => onChange({ ...DEFAULT_SUBTITLE_STYLE })}>Khôi phục mặc định</button>
        </fieldset>
        <div className="subtitle-preview-panel">
          <div className="subtitle-preview-heading"><span>Xem trước minh họa</span><label><input type="checkbox" checked={portrait} onChange={event => setPortrait(event.target.checked)} /> Video dọc</label></div>
          <div className={`subtitle-preview-frame${portrait ? ' portrait' : ''}`}><div className="subtitle-preview" style={previewStyle}><div style={textStyle}>{lines.slice(0, value.max_lines).map((text, index) => <div key={index}>{text}</div>)}</div></div></div>
          <label className="subtitle-sample">Nội dung xem thử<textarea value={sample} maxLength={500} rows={2} onChange={event => setSample(event.target.value)} /></label>
          <p>Font cần có trên máy hoặc trong thư mục font của ứng dụng. Nếu thiếu, video dùng font thay thế. Kích thước thực tế tự theo độ phân giải video; viền và bóng tính theo mốc 1080p.</p>
        </div>
      </div>
    </div>
  </details>
}
