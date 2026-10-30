export const DEFAULT_GREETING = 'Hallo allemaal, welkom!'

export type SharedOptions = {
  inputFolder: string
  outputFolder: string
  greeting: boolean
  greetingText: string
}

// Keeps the key of the earlier Settings-only draft so saved choices survive.
const STORAGE_KEY = 'videoauto.watchDraft'

/** Folder and greeting choices shared by the upload bar and Settings. */
export function readSharedOptions(): SharedOptions {
  let stored: Record<string, unknown> = {}
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) stored = parsed as Record<string, unknown>
  } catch {
    // Blocked or corrupt browser storage falls back to the defaults.
  }
  return {
    inputFolder: typeof stored.inputFolder === 'string' ? stored.inputFolder : '',
    outputFolder: typeof stored.outputFolder === 'string' ? stored.outputFolder : '',
    greeting: stored.greeting === true,
    greetingText: typeof stored.greetingText === 'string' ? stored.greetingText : DEFAULT_GREETING,
  }
}

export function writeSharedOptions(options: SharedOptions) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(options))
  } catch {
    // The choices still apply for this page; they just will not be remembered.
  }
}
