import type { BandInfo, Hosted, Worker } from '../types'

export type Band = { id: string; name?: string; primary: boolean; workers: Worker[] }

// Workers re-announce every 30s and are evicted after ~90s of silence.
export const STALE_SECS = 45

type Raw = Record<string, unknown>

const asRecord = (value: unknown): Raw =>
  value !== null && typeof value === 'object' ? (value as Raw) : {}

const hostedList = (value: unknown): Hosted[] =>
  (Array.isArray(value) ? value : []).map(one => {
    const raw = asRecord(one)

    return { name: String(raw.name ?? raw.url ?? '?'), url: String(raw.url ?? ''), note: String(raw.note ?? '') }
  })

// The hub's own worker announces on every band; rook lists it with band "*".
export const HUB_BAND = '*'

/** rook_workers answers a JSON array, sometimes wrapped as `{ result: "<json>" }`. */
export function parseWorkers(text: string): Worker[] {
  let data: unknown = JSON.parse(text)
  if (!Array.isArray(data) && typeof asRecord(data).result === 'string') {
    data = JSON.parse(asRecord(data).result as string)
  }
  if (!Array.isArray(data)) throw new Error('rook_workers: not a list')

  return data.map(one => {
    const raw = asRecord(one)
    const battery = asRecord(asRecord(raw.hb).battery)
    const serves = asRecord(raw.serves)
    const sites = hostedList(serves.sites)
    const services = hostedList(serves.services)

    return {
      id: String(raw.worker_id ?? ''),
      name: String(raw.name ?? '?'),
      description: String(raw.description ?? ''),
      band: String(raw.band ?? '?'),
      build: Number(raw.build ?? 0),
      ageSecs: Number(raw.last_seen_age_secs ?? 0),
      history: (Array.isArray(raw.plugins) ? raw.plugins : [])
        .map(String)
        .filter(plugin => plugin.endsWith('-history'))
        .map(plugin => plugin.slice(0, -'-history'.length))
        .sort(),
      ...(sites.length + services.length > 0 && { serves: { sites, services } }),
      ...(typeof battery.percent === 'number' && {
        battery: {
          percent: battery.percent,
          charging: battery.charging === true,
        },
      }),
    }
  })
}

/** rook_knowledge(action="bands") answers `{ ok, result: [{ name, label, primary }] }`, maybe wrapped. */
export function parseBands(text: string): BandInfo[] {
  let data: unknown = JSON.parse(text)
  if (typeof asRecord(data).result === 'string') {
    data = JSON.parse(asRecord(data).result as string)
  }
  const list = Array.isArray(data) ? data : asRecord(data).result
  if (!Array.isArray(list)) throw new Error('rook bands: not a list')

  return list.map(one => {
    const raw = asRecord(one)

    return {
      id: String(raw.id ?? ''),
      label: String(raw.label ?? ''),
      name: String(raw.name ?? ''),
      primary: raw.primary === true,
    }
  })
}

/**
 * The primary band first, then largest first; each band's workers by name.
 * A known band with no workers on it is listed too, empty.
 */
export function groupBands(workers: Worker[], known: BandInfo[] = []): Band[] {
  const bands = new Map<string, Worker[]>(known.map(info => [info.label, []]))
  for (const worker of workers) {
    bands.set(worker.band, [...(bands.get(worker.band) ?? []), worker])
  }

  return [...bands]
    .map(([id, list]) => {
      const info = known.find(one => one.label === id)

      return {
        id,
        ...(info !== undefined && info.name !== '' && { name: info.name }),
        ...(id === HUB_BAND && { name: 'Hub · all bands' }),
        primary: info?.primary === true,
        workers: [...list].sort((a, b) => a.name.localeCompare(b.name)),
      }
    })
    .sort(
      (a, b) =>
        Number(b.primary) - Number(a.primary) ||
        b.workers.length - a.workers.length ||
        (a.name ?? a.id).localeCompare(b.name ?? b.id),
    )
}

/**
 * The build most of the fleet runs. A worker below it is behind; one ahead of
 * it (a phone on a newer app release) does not make the rest look old.
 */
export function fleetBuild(workers: Worker[]): number {
  const counts = new Map<number, number>()
  for (const worker of workers) counts.set(worker.build, (counts.get(worker.build) ?? 0) + 1)

  return [...counts].sort((a, b) => b[1] - a[1] || b[0] - a[0])[0]?.[0] ?? 0
}
