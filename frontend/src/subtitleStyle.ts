export type SubtitleStyle = {
  font_family: string
  font_size: number
  color: string
  outline_color: string
  outline_width: number
  shadow: number
  alignment: number
  margin_v: number
  margin_h: number
  bold: boolean
  italic: boolean
  max_chars: number
  max_lines: number
}

export const DEFAULT_SUBTITLE_STYLE: SubtitleStyle = {
  font_family: 'Noto Sans', font_size: 5.5, color: '#FFFFFF', outline_color: '#000000',
  outline_width: 3, shadow: 1, alignment: 2, margin_v: 4.5, margin_h: 2.083333,
  bold: false, italic: false, max_chars: 42, max_lines: 2,
}

export const SUBTITLE_POSITIONS = [
  { value: 7, label: 'Trên trái' }, { value: 8, label: 'Trên giữa' }, { value: 9, label: 'Trên phải' },
  { value: 4, label: 'Giữa trái' }, { value: 5, label: 'Chính giữa' }, { value: 6, label: 'Giữa phải' },
  { value: 1, label: 'Dưới trái' }, { value: 2, label: 'Dưới giữa' }, { value: 3, label: 'Dưới phải' },
]

export function normalizeSubtitleStyle(value?: Partial<SubtitleStyle>): SubtitleStyle {
  const style = { ...DEFAULT_SUBTITLE_STYLE, ...value }
  // Explicit order makes upload identities independent of JSON property order.
  return {
    font_family: style.font_family.trim() || DEFAULT_SUBTITLE_STYLE.font_family, font_size: style.font_size,
    color: style.color.toUpperCase(), outline_color: style.outline_color.toUpperCase(),
    outline_width: style.outline_width, shadow: style.shadow, alignment: style.alignment,
    margin_v: style.margin_v, margin_h: style.margin_h, bold: style.bold, italic: style.italic,
    max_chars: style.max_chars, max_lines: style.max_lines,
  }
}

export function isSubtitleStyle(value: unknown): value is Partial<SubtitleStyle> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const ranges: Record<string, [number, number]> = {
    font_size: [1, 15], outline_width: [0, 8], shadow: [0, 8], alignment: [1, 9],
    margin_v: [0, 30], margin_h: [0, 30], max_chars: [10, 100], max_lines: [1, 4],
  }
  return Object.entries(value).every(([key, item]) => {
    if (key === 'font_family') return typeof item === 'string' && item.trim().length > 0 && item.length <= 80 && !/[,\\{}\x00-\x1f\x7f]/.test(item)
    if (key === 'color' || key === 'outline_color') return typeof item === 'string' && /^#[0-9a-f]{6}$/i.test(item)
    if (key === 'bold' || key === 'italic') return typeof item === 'boolean'
    const range = ranges[key]
    return !!range && typeof item === 'number' && Number.isFinite(item) && item >= range[0] && item <= range[1] &&
      (!['alignment', 'max_chars', 'max_lines'].includes(key) || Number.isInteger(item))
  })
}
