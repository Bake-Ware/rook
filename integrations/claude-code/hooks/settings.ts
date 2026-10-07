import type { ConfigRow, ConfigValue } from 'claude-code'

import type { SettingRow } from '../types'

/**
 * Claude Code's own setting for messages other sessions send into this one,
 * which is how a rook poke (claude-history.send) arrives.
 */
export const INBOUND = 'crossSessionInbound'

/** What each value of the inbound setting does to a rook poke. */
export const INBOUND_HELP: Readonly<Record<string, string>> = {
  default: 'Claude Code decides: delivered, but held for your OK while permissions are bypassed.',
  accept: 'Delivered straight into this chat: a rook poke starts a turn without a click.',
  hold: 'Held until you approve each one.',
  refuse: 'Dropped.',
}

/** Where the person changes the inbound setting when Claude Code refuses a plugin. */
export const INBOUND_MENU = 'open /config and pick it under "Messages from your other sessions"'

/** The rows the settings tab shows: the inbound setting first, then rook's own options. */
export function pickSettings(rows: readonly ConfigRow[]): SettingRow[] {
  const wanted = rows.filter(row => row.key === INBOUND || row.key.startsWith('rook.'))

  return [...wanted]
    .sort((a, b) => Number(b.key === INBOUND) - Number(a.key === INBOUND))
    .map(row => ({
      key: row.key,
      label: row.label,
      ...(row.key === INBOUND
        ? { description: 'How a message from another session arrives here, including a rook poke.' }
        : row.description !== undefined
          ? { description: row.description }
          : {}),
      kind: row.kind,
      value: row.value,
      ...(row.options !== undefined ? { options: [...row.options] } : {}),
      locked: row.isLocked,
    }))
}

export const showValue = (value: ConfigValue): string =>
  typeof value === 'string'
    ? value === ''
      ? '(not set)'
      : value
    : Array.isArray(value)
      ? value.join(', ')
      : String(value)

/** The settings tab as plain text, for the pane tool. */
export function settingsLines(rows: readonly SettingRow[] | undefined): string[] {
  if (rows === undefined) return ['Loading settings…']
  if (rows.length === 0) return ['No settings to show.']

  return rows.flatMap(row => {
    const help = row.key === INBOUND && typeof row.value === 'string' ? INBOUND_HELP[row.value] : undefined

    return [
      '',
      `## ${row.label} (${row.key})${row.locked ? ' · locked by policy' : ''}`,
      ...(row.description !== undefined ? [row.description] : []),
      `value: ${showValue(row.value)}`,
      ...(row.options !== undefined ? [`options: ${row.options.join(', ')}`] : []),
      ...(help !== undefined ? [help] : []),
    ]
  })
}
