/**
 * POW Tracker — Hermes desktop plugin pane (upgraded).
 *
 * Live time-tracking dashboard for Smart Polar Distributors. Dark theme,
 * pulse/glow live dots, smooth area charts + per-task sparklines, per-entry
 * editing, invoice date-range picker, billable vs non-billable totals.
 *
 * Plain ESM (jsx()/jsxs() calls, no JSX syntax). Imports ONLY
 * @hermes/plugin-sdk + react/jsx-runtime. Talks to the backend via
 * ctx.rest / ctx.socket (auto-scoped). Export URLs keep the existing
 * window.hermesDesktop.getConnection(profile).token pattern — tokens are
 * never logged or printed.
 */

import {
  atom,
  Button,
  cn,
  haptic,
  host,
  Input,
  PALETTE_AREA,
  PANES_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Tip,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'pow-tracker'
const REST_MS = 4000          // polling fallback when WS unavailable
const TICK_MS = 1000          // live clock tick

// ── State atoms ────────────────────────────────────────────────────────────────
const $state = atom(null)         // full backend snapshot (/state)
const $stateError = atom('')
const $connected = atom(false)    // WS or poll healthy
const $view = atom('track')       // track | entries | tasks | invoice | rate
const $selTask = atom('')         // selected task for the timer
const $now = atom(Date.now() / 1000)
const $resting = atom(false)      // global busy guard for mutations
const $clients = atom([])         // client registry rows
const $client = atom(null)        // active client row

// ── REST helpers ───────────────────────────────────────────────────────────────
async function rest(ctx, path, opts) {
  return ctx.rest(path, opts)
}

async function refreshState(ctx) {
  try {
    const s = await rest(ctx, '/state')
    $state.set(s)
    $stateError.set('')
    $connected.set(true)
  } catch (err) {
    $stateError.set(String(err))
    $connected.set(false)
  }
}

async function refreshClients(ctx) {
  try {
    const r = await rest(ctx, '/clients')
    $clients.set(r.clients || [])
    $client.set((r.clients || []).find((c) => c.active) || null)
  } catch { /* registry unavailable — keep last */ }
}

async function switchClient(ctx, id) {
  $resting.set(true)
  try {
    await rest(ctx, `/clients/${id}/activate`, { method: 'POST', body: {} })
    await Promise.all([refreshClients(ctx), refreshState(ctx)])
  } catch (e) { $stateError.set(String(e)) }
  finally { $resting.set(false) }
}

async function addClient(ctx, name) {
  $resting.set(true)
  try {
    await rest(ctx, '/clients', { method: 'POST', body: { name } })
    const r = await rest(ctx, '/clients')
    const created = (r.clients || []).find((c) => c.name === name)
    if (created) await rest(ctx, `/clients/${created.id}/activate`, { method: 'POST', body: {} })
    await Promise.all([refreshClients(ctx), refreshState(ctx)])
  } catch (e) { $stateError.set(String(e)) }
  finally { $resting.set(false) }
}

// Build a download URL using the active connection's session token.
// The token is taken from the existing gateway connection object and placed
// only in the URL — never logged, never stored in state.
function exportUrl(ctx, path) {
  try {
    const profile = (host.state && host.state.profile && host.state.profile.get)
      ? host.state.profile.get()
      : ''
    const conn = window.hermesDesktop.getConnection(profile)
    if (!conn || !conn.baseUrl) return '/api/plugins/' + ID + path
    return `${conn.baseUrl}/api/plugins/${ID}${path}?token=${encodeURIComponent(conn.token)}`
  } catch (e) {
    return '/api/plugins/' + ID + path
  }
}

// ── Formatters ────────────────────────────────────────────────────────────────
function fmtDuration(s) {
  if (s == null || !isFinite(s)) return '0s'
  s = Math.max(0, Math.floor(s))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${sec}s`
  return `${sec}s`
}

function fmtMoney(n, cur) {
  if (n == null || !isFinite(n)) return (cur || 'NAD') + ' 0.00'
  return `${cur || 'NAD'} ${Number(n).toFixed(2)}`
}

function fmtClock(ts) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString()
}

// Local-date → UTC-epoch helper for the invoice range picker (end of day).
function dayStartEpoch(localDateStr) {
  if (!localDateStr) return null
  const d = new Date(localDateStr + 'T00:00:00')
  return isNaN(d) ? null : d.getTime() / 1000
}
function dayEndEpoch(localDateStr) {
  if (!localDateStr) return null
  const d = new Date(localDateStr + 'T23:59:59.999')
  return isNaN(d) ? null : d.getTime() / 1000
}

const mono = 'font-mono tabular-nums'

// ── Styles ────────────────────────────────────────────────────────────────────
const CSS = [
  '@keyframes spd-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }',
  '@keyframes spd-fadeup { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }',
  '@keyframes spd-draw { to { stroke-dashoffset: 0; } }',
  '.spd-live { animation: spd-pulse 1.6s ease-in-out infinite; }',
  '.spd-fadeup { animation: spd-fadeup 0.25s ease-out both; }',
  '.spd-bar { transition: width 0.6s cubic-bezier(0.4,0,0.2,1); }'
].join('\n')

function StyleTag() {
  return jsx('style', { children: CSS })
}

// ── Smooth area chart with animated draw-in ───────────────────────────────────
function AreaChart({ values, height = 90, color }) {
  const ref = useRef(null)
  const [w, setW] = useState(280)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) setW(Math.max(120, e.contentRect.width))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  if (!values || values.length < 2) {
    return jsx('div', { ref, className: cn('text-xs text-(--ui-text-tertiary)', mono), children: 'no data yet' })
  }
  const H = height
  const padX = 4, padTop = 8, padBottom = 14
  const maxV = Math.max(...values, 1)
  const stepX = (w - padX * 2) / (values.length - 1)
  const pts = values.map((v, i) => ({
    x: padX + i * stepX,
    y: padTop + (1 - v / maxV) * (H - padTop - padBottom)
  }))
  // Catmull-Rom → cubic bezier smoothing (same as original, guarded).
  let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)]
    const c1x = p1.x + (p2.x - p0.x) / 6, c1y = p1.y + (p2.y - p0.y) / 6
    const c2x = p2.x - (p3.x - p1.x) / 6, c2y = p2.y - (p3.y - p1.y) / 6
    d += ` C ${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`
  }
  const area = `${d} L ${pts[pts.length - 1].x.toFixed(1)} ${(H - padBottom).toFixed(1)} L ${pts[0].x.toFixed(1)} ${(H - padBottom).toFixed(1)} Z`
  const last = pts[pts.length - 1]
  const gid = 'spd-grad-' + Math.abs(values.length * 31 + H)
  return jsxs('svg', {
    ref,
    viewBox: `0 0 ${w} ${H}`,
    className: 'w-full overflow-visible',
    children: [
      jsxs('defs', {
        children: jsx('linearGradient', { id: gid, x1: '0', y1: '0', x2: '0', y2: '1', children: [
          jsx('stop', { offset: '0%', stopColor: color, stopOpacity: '0.35' }),
          jsx('stop', { offset: '100%', stopColor: color, stopOpacity: '0.02' })
        ] })
      }),
      jsx('path', { d: area, fill: `url(#${gid})`, stroke: 'none' }),
      jsx('path', {
        d, fill: 'none', stroke: color, strokeWidth: 2,
        strokeLinecap: 'round', strokeLinejoin: 'round',
        pathLength: 1, style: { strokeDasharray: 1, strokeDashoffset: 1 },
        className: 'spd-draw'
      }),
      jsx('circle', { cx: last.x, cy: last.y, r: 3.5, fill: color, className: 'spd-live' })
    ]
  })
}

// Tiny per-row sparkline (hours trend), no axes, single polyline.
function Sparkline({ values, color = 'var(--ui-accent)' }) {
  if (!values || values.length < 2) return null
  const w = 56, hgt = 16
  const maxV = Math.max(...values, 0.001)
  const step = w / (values.length - 1)
  const d = values.map((v, i) => `${i ? 'L' : 'M'} ${(i * step).toFixed(1)} ${(hgt - (v / maxV) * (hgt - 2)).toFixed(1)}`).join(' ')
  return jsx('svg', { width: w, height: hgt, viewBox: `0 0 ${w} ${hgt}`, className: 'inline-block', children:
    jsx('path', { d, fill: 'none', stroke: color, strokeWidth: 1.5, strokeLinecap: 'round', opacity: 0.8 })
  })
}

// ── Header chip ───────────────────────────────────────────────────────────────
function LiveDot({ on, color }) {
  return jsx('span', {
    className: cn('inline-block h-2 w-2 rounded-full', on && 'spd-live'),
    style: { backgroundColor: color, boxShadow: on ? `0 0 6px 1px ${color}aa` : undefined }
  })
}

function Header({ ctx, state, connected }) {
  const runCount = (state && state.counts && state.counts.running) || 0
  const clients = useValue($clients)
  const active = useValue($client)
  return jsxs('div', {
    className: 'flex items-center justify-between gap-2',
    children: [
      jsxs('div', { className: 'flex items-center gap-2', children: [
        jsx('i', { className: 'i-codicon-clock text-(--ui-accent)' }),
        jsxs('span', { className: 'text-sm font-semibold', children: ['POW Tracker'] }),
        jsx(LiveDot, { on: connected, color: connected ? '#22c55e' : '#ef4444' })
      ] }),
      jsxs('div', { className: 'flex items-center gap-2', children: [
        jsx(Select, {
          value: active ? active.id : '',
          onValueChange: (v) => switchClient(ctx, v),
          children: [
            jsx(SelectTrigger, { className: 'h-6 w-40 text-[0.6875rem]', children:
              jsx(SelectValue, { placeholder: active ? active.name : 'Client…' }) }),
            jsx(SelectContent, { children:
              clients.map((c) => jsx(SelectItem, { key: c.id, value: c.id,
                children: c.name + (c.active ? ' ●' : '') }))
            })
          ]
        }),
        jsxs('span', {
          className: cn('rounded px-2 py-0.5 text-[0.625rem] font-bold uppercase', mono),
          style: {
            backgroundColor: runCount ? '#f59e0b22' : 'rgba(148,163,184,0.15)',
            color: runCount ? '#f59e0b' : 'var(--ui-text-tertiary)'
          },
          children: [runCount ? `${runCount} running` : 'idle']
        })
      ] })
    ]
  })
}

// ── Task select (controlled across renders) ───────────────────────────────────
function TaskSelect({ value, tasks, onChange }) {
  const [val, setVal] = useState(value)
  useEffect(() => { setVal(value) }, [value])
  return jsxs('div', { className: 'flex-1', children:
    jsxs(Select, {
      value: val,
      onValueChange: (v) => { setVal(v); onChange(v) },
      children: [
        jsx(SelectTrigger, { className: 'h-8 w-full text-xs', children: jsx(SelectValue, { placeholder: 'Select task' }) }),
        jsx(SelectContent, {
          children: tasks.filter((t) => !t.archived).map((t) =>
            jsx(SelectItem, { key: t.id, value: t.id, children: t.name }))
        })
      ]
    })
  })
}

// ── Track view ────────────────────────────────────────────────────────────────
function TimerPanel({ ctx, state }) {
  const selTask = useValue($selTask)
  const resting = useValue($resting)
  const [taskName, setTaskName] = useState('')
  const settings = (state && state.settings) || {}
  const rate = (state && state.rate_nad) || 0
  const tasks = (state && state.tasks) || []
  const running = (state && state.running) || []
  const firstActive = tasks.find((t) => !t.archived)
  const effectiveTask = selTask || (firstActive && firstActive.id) || ''

  useEffect(() => { if (!selTask && effectiveTask) $selTask.set(effectiveTask) }, [effectiveTask, selTask])

  async function startTimer(tid) {
    if (!tid) return
    $resting.set(true)
    try {
      await rest(ctx, '/entries', { method: 'POST', body: { task_id: tid, note: '', source: 'manual' } })
      await refreshState(ctx)
    } catch (e) { $stateError.set(String(e)) }
    finally { $resting.set(false) }
  }
  async function stopTimer(eid, note) {
    $resting.set(true)
    try {
      await rest(ctx, `/entries/${eid}/stop`, { method: 'POST', body: note ? { note } : {} })
      await refreshState(ctx)
    } catch (e) { $stateError.set(String(e)) }
    finally { $resting.set(false) }
  }

  return jsxs('div', { className: 'flex flex-col gap-3', children: [
    running.length
      ? running.map((e) => jsx(RunningCard, { key: e.id, entry: e, rate, cur: settings.currency, onStop: stopTimer }))
      : null,

    jsxs('div', { className: 'flex items-center gap-2', children: [
      jsx(Input, {
        className: 'h-8 flex-1 text-xs',
        placeholder: 'New task name…',
        value: taskName,
        onChange: (e) => setTaskName(e.target.value)
      }),
      jsx(Button, {
        className: 'h-8', variant: 'ghost',
        disabled: !taskName.trim() || resting,
        onClick: async () => {
          if (!taskName.trim()) return
          $resting.set(true)
          try {
            const r = await rest(ctx, '/tasks', { method: 'POST', body: { name: taskName.trim() } })
            setTaskName('')
            $selTask.set(r.task.id)
            await refreshState(ctx)
          } catch (e) { $stateError.set(String(e)) }
          finally { $resting.set(false) }
        },
        children: 'Add task'
      })
    ] }),

    jsxs('div', { className: 'flex items-center gap-2', children: [
      tasks.length
        ? jsx(TaskSelect, { value: effectiveTask, tasks, onChange: (v) => $selTask.set(v) })
        : jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Add a task first' }),
      jsx(Button, {
        className: 'h-8',
        disabled: !effectiveTask || resting,
        onClick: () => startTimer(effectiveTask),
        children: running.length ? 'Another timer…' : 'Start timer'
      })
    ] }),

    jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children:
      `Rate: ${fmtMoney(rate, settings.currency)} / hr · Flat across tasks`
    })
  ] })
}

// Live timer card — supports multiple simultaneous timers; unsaved note is
// flushed on stop.
function RunningCard({ entry, rate, cur, onStop }) {
  const [note, setNote] = useState(entry.note || '')
  const [tick, setTick] = useState(Math.floor(Date.now() / 1000 - entry.start_ts))
  useEffect(() => {
    const t = setInterval(() => setTick(Math.floor(Date.now() / 1000 - entry.start_ts)), TICK_MS)
    return () => clearInterval(t)
  }, [entry.start_ts])
  const amount = (tick / 3600) * (rate || 0)
  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-primary)/60 p-3 spd-fadeup',
    style: { borderColor: '#f59e0b55' },
    children: [
      jsxs('div', { className: 'flex items-center justify-between', children: [
        jsxs('span', { className: 'flex items-center gap-1.5 text-xs font-semibold', style: { color: '#f59e0b' }, children: [
          jsx(LiveDot, { on: true, color: '#f59e0b' }),
          'tracking · ', entry.task_name || ''
        ] }),
        jsx('span', { className: cn('text-lg font-bold', mono), style: { color: '#f59e0b' }, children: fmtDuration(tick) })
      ] }),
      jsx(Input, {
        className: 'h-7 text-xs',
        placeholder: 'What are you working on?',
        value: note,
        onChange: (e) => setNote(e.target.value)
      }),
      jsxs('div', { className: 'flex items-center justify-between', children: [
        jsx('span', { className: cn('text-[0.6875rem] text-(--ui-text-tertiary)', mono), children: fmtMoney(amount, cur || 'NAD') }),
        jsx(Button, { className: 'h-7', variant: 'default', onClick: () => onStop(entry.id, note), children: 'Stop & bill' })
      ] })
    ]
  })
}

// ── Entries view (per-entry editing) ──────────────────────────────────────────
function EntriesView({ ctx, state }) {
  const entries = (state && state.entries) || []
  const settings = (state && state.settings) || {}
  const cur = settings.currency || 'NAD'
  const resting = useValue($resting)
  const [editingId, setEditingId] = useState(null)
  const [draft, setDraft] = useState({})
  const [filter, setFilter] = useState('all') // all | billable | nonbillable

  function beginEdit(e) {
    setEditingId(e.id)
    setDraft({
      note: e.note || '',
      date: e.start_ts ? new Date(e.start_ts * 1000).toISOString().slice(0, 10) : '',
      hours: e.duration_s != null ? (e.duration_s / 3600).toFixed(3) : '',
      rate: e.rate_nad != null ? String(e.rate_nad) : '',
      billable: true // refined below from task
    })
    const task = ((state && state.tasks) || []).find((t) => t.id === e.task_id)
    setDraft((d) => ({ ...d, billable: task ? !!task.billable : true }))
  }

  async function saveEdit(id) {
    $resting.set(true)
    try {
      const body = {}
      if (draft.note !== undefined) body.note = draft.note
      if (draft.hours !== '' && isFinite(parseFloat(draft.hours))) body.duration_s = parseFloat(draft.hours) * 3600
      if (draft.date) {
        const start = dayStartEpoch(draft.date)
        if (start != null) body.start_ts = start
      }
      if (draft.rate !== '') body.rate_nad = parseFloat(draft.rate)
      await rest(ctx, `/entries/${id}`, { method: 'PATCH', body })
      setEditingId(null)
      await refreshState(ctx)
    } catch (e) { $stateError.set(String(e)) }
    finally { $resting.set(false) }
  }

  async function delEntry(id) {
    $resting.set(true)
    try {
      await rest(ctx, `/entries/${id}`, { method: 'DELETE' })
      await refreshState(ctx)
    } catch (e) { $stateError.set(String(e)) }
    finally { $resting.set(false) }
  }

  const visible = entries.filter((e) =>
    filter === 'all' ? true : filter === 'billable' ? e.amount_nad != null : e.amount_nad == null)

  return jsxs('div', { className: 'flex flex-col gap-2', children: [
    jsxs('div', { className: 'flex gap-1', children:
      ['all', 'billable', 'nonbillable'].map((f) =>
        jsx('button', {
          key: f, type: 'button',
          onClick: () => setFilter(f),
          className: cn('h-6 rounded px-2 text-[0.625rem] font-medium',
            filter === f ? 'bg-(--ui-accent) text-(--ui-on-accent)' : 'bg-(--ui-bg-secondary) text-(--ui-text-secondary)'),
          children: f
        }))
    }),
      visible.length === 0 ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'No entries.' }) : null,
      visible.slice(0, 60).map((e) =>
        editingId === e.id
          ? jsxs('div', { key: e.id, className: 'flex flex-col gap-1.5 rounded-md border border-(--ui-accent)/40 bg-(--ui-bg-primary)/70 px-2.5 py-2', children: [
            jsx(Input, { className: 'h-7 text-xs', placeholder: 'Note', value: draft.note, onChange: (ev) => setDraft({ ...draft, note: ev.target.value }) }),
            jsxs('div', { className: 'flex items-center gap-1.5', children: [
              jsx(Input, { className: 'h-7 flex-1 text-xs', type: 'date', value: draft.date, onChange: (ev) => setDraft({ ...draft, date: ev.target.value }) }),
              jsx(Input, { className: 'h-7 w-20 text-xs', type: 'number', step: '0.01', placeholder: 'hours', value: draft.hours, onChange: (ev) => setDraft({ ...draft, hours: ev.target.value }) }),
              jsx(Input, { className: 'h-7 w-20 text-xs', type: 'number', placeholder: 'rate', value: draft.rate, onChange: (ev) => setDraft({ ...draft, rate: ev.target.value }) })
            ] }),
            jsxs('div', { className: 'flex justify-end gap-1', children: [
              jsx(Button, { className: 'h-6 px-2 text-[0.6875rem]', variant: 'default', disabled: resting, onClick: () => saveEdit(e.id), children: 'Save' }),
              jsx(Button, { className: 'h-6 px-2 text-[0.6875rem]', variant: 'ghost', onClick: () => setEditingId(null), children: 'Cancel' })
            ] })
          ] })
          : jsxs('div', { key: e.id, className: 'group flex items-center justify-between rounded-md bg-(--ui-bg-primary)/40 px-2.5 py-1.5', children: [
            jsxs('div', { className: 'min-w-0 flex-1', children: [
              jsxs('div', { className: 'flex items-center gap-2', children: [
                jsx('span', { className: cn('truncate text-xs', e.running && 'text-(--ui-warning)'), children: e.note || e.task_name || '(untitled)' }),
                e.running ? jsx(LiveDot, { on: true, color: '#f59e0b' }) : null
              ] }),
              jsx('div', { className: cn('text-[0.625rem] text-(--ui-text-tertiary)', mono), children:
                `${fmtClock(e.start_ts)}${e.end_ts ? '–' + fmtClock(e.end_ts) : ''} · ${fmtDuration(e.duration_s ?? (Date.now() / 1000 - e.start_ts))}`
              })
            ] }),
            jsxs('div', { className: 'flex items-center gap-1', children: [
              e.amount_nad != null
                ? jsx('span', { className: cn('mr-1 font-semibold', mono, 'text-xs'), children: fmtMoney(e.amount_nad, cur) })
                : jsx('span', { className: 'mr-1 text-[0.625rem] text-(--ui-text-tertiary)', children: 'non-billable' }),
              jsx(Tip, { content: 'Edit entry', children:
                jsx(Button, { variant: 'ghost', className: 'h-6 px-1.5 text-[0.6875rem]', onClick: () => beginEdit(e), children: '✎' })
              }),
              jsx(Button, { variant: 'ghost', className: 'h-6 px-1.5 text-[0.6875rem] text-(--ui-danger)', onClick: () => delEntry(e.id), children: '✕' })
            ] })
          ] })
      )
    ] })
}

// ── Tasks view ────────────────────────────────────────────────────────────────
function TasksView({ ctx, state }) {
  const tasks = (state && state.tasks) || []
  const entries = (state && state.entries) || []
  const rate = (state && state.rate_nad) || 0
  const cur = ((state && state.settings) || {}).currency || 'NAD'
  const resting = useValue($resting)

  async function archive(t) {
    $resting.set(true)
    try { await rest(ctx, `/tasks/${t.id}`, { method: 'PATCH', body: { archived: !t.archived } }); await refreshState(ctx) }
    finally { $resting.set(false) }
  }
  async function rename(t) {
    const name = prompt('Rename task', t.name)
    if (!name || !name.trim()) return
    $resting.set(true)
    try { await rest(ctx, `/tasks/${t.id}`, { method: 'PATCH', body: { name: name.trim() } }); await refreshState(ctx) }
    finally { $resting.set(false) }
  }
  async function toggleBillable(t) {
    $resting.set(true)
    try { await rest(ctx, `/tasks/${t.id}`, { method: 'PATCH', body: { billable: !t.billable } }); await refreshState(ctx) }
    finally { $resting.set(false) }
  }
  async function del(t) {
    if (!window.confirm(`Delete "${t.name}" and all its entries? This cannot be undone.`)) return
    $resting.set(true)
    try { await rest(ctx, `/tasks/${t.id}`, { method: 'DELETE' }); await refreshState(ctx) }
    catch (e) { $stateError.set(String(e)) }
    finally { $resting.set(false) }
  }

  const perTask = {}
  const perTaskHoursTrend = {}
  entries.forEach((e) => {
    if (e.running) return
    perTask[e.task_id] = (perTask[e.task_id] || 0) + (e.duration_s || 0)
    const day = new Date(e.start_ts * 1000).toISOString().slice(0, 10)
    const k = e.task_id + '|' + day
    perTaskHoursTrend[k] = (perTaskHoursTrend[k] || 0) + (e.duration_s || 0)
  })

  function trendFor(taskId) {
    const days = Object.keys(perTaskHoursTrend)
      .filter((k) => k.startsWith(taskId + '|'))
      .sort()
      .slice(-14)
      .map((k) => perTaskHoursTrend[k] / 3600)
    return days
  }

  return jsxs('div', { className: 'flex flex-col gap-2', children: [
    tasks.length === 0 ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'No tasks yet.' }) : null,
    tasks.map((t) => {
      const sec = perTask[t.id] || 0
      return jsxs('div', {
        key: t.id,
        className: cn('flex items-center justify-between rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-primary)/40 px-3 py-2', t.archived && 'opacity-50'),
        children: [
          jsxs('div', { className: 'min-w-0 flex-1', children: [
            jsxs('div', { className: 'flex items-center gap-2', children: [
              jsx('span', { className: 'truncate text-xs font-medium', children: t.name }),
              jsx('button', { type: 'button', onClick: () => toggleBillable(t), className:
                cn('rounded px-1 text-[0.625rem]', t.billable ? 'bg-emerald-500/15 text-emerald-400' : 'bg-(--ui-bg-secondary) text-(--ui-text-tertiary)'),
                children: t.billable ? 'billable' : 'non-bill' })
            ] }),
            jsxs('div', { className: 'flex items-center gap-2', children: [
              jsx('span', { className: cn('text-[0.625rem] text-(--ui-text-tertiary)', mono), children:
                `${fmtDuration(sec)} · ${fmtMoney((sec / 3600) * (t.billable ? rate : 0), cur)}` }),
              jsx(Sparkline, { values: trendFor(t.id) })
            ] })
          ] }),
          jsxs('div', { className: 'flex items-center gap-1', children: [
            jsx(Button, { variant: 'ghost', className: 'h-6 px-2 text-[0.6875rem]', onClick: () => rename(t), children: '✎' }),
            jsx(Button, { variant: 'ghost', className: 'h-6 px-2 text-[0.6875rem]', onClick: () => archive(t), children: t.archived ? 'Restore' : 'Archive' }),
            jsx(Button, { variant: 'ghost', className: 'h-6 px-2 text-[0.6875rem] text-(--ui-danger)', onClick: () => del(t), children: 'Delete' })
          ] })
        ]
      })
    })
  ] })
}

// ── Invoice view ──────────────────────────────────────────────────────────────
function InvoiceView({ ctx, state }) {
  const [inv, setInv] = useState(null)
  const [loadErr, setLoadErr] = useState('')
  const settings = (state && state.settings) || {}
  const cur = settings.currency || 'NAD'
  const [fromD, setFromD] = useState('')
  const [toD, setToD] = useState('')

  async function load(range) {
    setLoadErr('')
    try {
      const qs = []
      if (range && range.from) qs.push('from_ts=' + encodeURIComponent(dayStartEpoch(range.from)))
      if (range && range.to) qs.push('to_ts=' + encodeURIComponent(dayEndEpoch(range.to)))
      const q = qs.length ? '?' + qs.join('&') : ''
      const i = await rest(ctx, '/invoice' + q)
      setInv(i)
    } catch (e) { setLoadErr(String(e)) }
  }
  useEffect(() => { load(null) }, [])

  const chartVals = useMemo(() => (inv ? inv.lines.map((l) => l.hours || 0) : []), [inv])
  const maxHours = Math.max(0.001, ...chartVals)

  return jsxs('div', { className: 'flex flex-col gap-3', children: [
    jsxs('div', { className: 'grid grid-cols-3 gap-2', children: [
      Stat('Total hours', inv ? inv.total_hours.toFixed(2) : '—'),
      Stat('Billable due', inv ? fmtMoney(inv.total_nad, cur) : '—'),
      Stat('Non-billable h', inv ? String(inv.nonbillable_hours ?? '0') : '—')
    ] }),

    // Invoice date-range picker wired to /invoice?from_ts&to_ts.
    jsxs('div', { className: 'flex items-end gap-1.5', children: [
      jsx(Input, { className: 'h-8 flex-1 text-xs', type: 'date', value: fromD, onChange: (e) => setFromD(e.target.value) }),
      jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: '→' }),
      jsx(Input, { className: 'h-8 flex-1 text-xs', type: 'date', value: toD, onChange: (e) => setToD(e.target.value) }),
      jsx(Button, { className: 'h-8', variant: 'default', onClick: () => load({ from: fromD, to: toD }), children: 'Apply' }),
      jsx(Button, { className: 'h-8', variant: 'ghost', onClick: () => { setFromD(''); setToD(''); load(null) }, children: 'All' })
    ] }),

    chartVals.length ? jsx(AreaChart, { values: chartVals.map((v) => v / maxHours), color: 'var(--ui-accent)' }) : null,
    jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'Hours per task (auto-scaled)' }),

    inv && inv.lines.length
      ? jsxs('div', { className: 'flex flex-col gap-1', children:
          inv.lines.map((l) => jsxs('div', {
            key: l.task_id,
            className: 'flex items-center justify-between rounded-md bg-(--ui-bg-primary)/40 px-2.5 py-1.5 text-xs',
            children: [
              jsxs('div', { className: 'min-w-0 flex-1', children: [
                jsx('span', { className: 'truncate font-medium', children: l.task_name }),
                jsx('span', { className: cn('ml-2 text-[0.625rem] text-(--ui-text-tertiary)', mono), children: `${l.hours.toFixed(2)}h` })
              ] }),
              jsx('span', { className: cn('font-semibold', mono), children: fmtMoney(l.amount_nad, cur) })
            ]
          })) })
      : jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'No billable time yet.' }),

    loadErr ? jsx('div', { className: 'text-xs text-(--ui-danger)', children: loadErr }) : null,

    jsxs('div', { className: 'flex items-center gap-2 pt-1', children: [
      jsx(Button, { className: 'h-8 flex-1', variant: 'ghost', onClick: () => load(fromD || toD ? { from: fromD, to: toD } : null), children: 'Refresh' }),
      jsx(Button, { className: 'h-8 flex-1', variant: 'default', onClick: () => { window.open(exportUrl(ctx, '/invoice.pdf'), '_blank') }, children: 'PDF' }),
      jsx(Button, { className: 'h-8 flex-1', variant: 'default', onClick: () => { window.open(exportUrl(ctx, '/invoice.csv'), '_blank') }, children: 'CSV' })
    ] }),
    jsx(Button, { className: 'h-8', variant: 'ghost', onClick: () => { window.open(exportUrl(ctx, '/export.csv'), '_blank') }, children: 'Export all entries (CSV)' })
  ] })
}

function Stat(label, value) {
  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-primary)/50 p-3',
    children: [
      jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: label }),
      jsx('span', { className: cn('text-lg font-semibold', mono), children: value })
    ]
  })
}

// ── Settings (rate + auto-capture + clients) ─────────────────────────────────
function SettingsRow({ ctx, state }) {
  const settings = (state && state.settings) || {}
  // Draft state is ONLY initialised once and NEVER re-synced from server state
  // while editing — the old version reset the field on every 4s poll, wiping
  // input before Save could be clicked. Save-on-blur/Enter plus explicit sync
  // only when idle keeps typed values intact.
  const [rate, setRate] = useState(() => String((state && state.rate_nad) != null ? state.rate_nad : 0))
  const [auto, setAuto] = useState((settings.auto_capture === '1'))
  const [autoTask, setAutoTask] = useState(settings.auto_task_id || '')
  const [idle, setIdle] = useState(settings.auto_idle_s || 900)
  const [savedFlash, setSavedFlash] = useState(false)
  const dirtyRef = useRef(false)
  const tasks = (state && state.tasks) || []
  const clients = useValue($clients)
  const activeClient = useValue($client)
  const [newClient, setNewClient] = useState('')

  async function patchSettings(body) {
    try {
      await rest(ctx, '/settings', { method: 'PATCH', body })
      await refreshState(ctx)
      setSavedFlash(true)
      setTimeout(() => setSavedFlash(false), 1500)
    } catch (e) { $stateError.set(String(e)) }
  }

  function saveRate() {
    dirtyRef.current = false
    patchSettings({ rate_nad: parseFloat(rate) || 0 })
  }

  return jsxs('div', { className: 'flex flex-col gap-3', children: [
    jsxs('div', { className: 'flex items-center gap-2', children: [
      jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: 'Hourly rate' }),
      jsx(Input, {
        className: 'h-8 w-28 text-xs', type: 'number',
        value: rate,
        onChange: (e) => { dirtyRef.current = true; setRate(e.target.value) },
        onBlur: () => { if (dirtyRef.current) saveRate() },
        onKeyDown: (e) => { if (e.key === 'Enter') saveRate() }
      }),
      jsx(Button, { className: 'h-8', variant: 'ghost', onClick: saveRate, children: 'Save' }),
      savedFlash ? jsx('span', { className: 'text-[0.625rem] text-emerald-400', children: 'saved ✓' }) : null
    ] }),

    jsxs('div', { className: 'flex items-center justify-between rounded-md bg-(--ui-bg-primary)/40 px-3 py-2', children: [
      jsxs('div', { children: [
        jsx('span', { className: 'text-xs font-medium', children: 'Auto-capture from folder' }),
        jsx('div', { className: 'text-[0.625rem] text-(--ui-text-tertiary)', children: 'Open timer on client folder activity' })
      ] }),
      jsx(Switch, { checked: auto, onCheckedChange: (v) => { setAuto(v); setTimeout(() => patchSettings({ auto_capture: v }), 0) } })
    ] }),
    auto ? jsxs('div', { className: 'flex flex-col gap-2', children: [
      jsx(TaskSelect, { value: autoTask, tasks, onChange: (v) => { setAutoTask(v); setTimeout(() => patchSettings({ auto_task_id: v }), 0) } }),
      jsxs('div', { className: 'flex items-center gap-2', children: [
        jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'Idle close (s)' }),
        jsx(Input, { className: 'h-8 w-24 text-xs', type: 'number', min: '60', value: idle, onChange: (e) => setIdle(e.target.value) }),
        jsx(Button, { className: 'h-8', variant: 'ghost', onClick: () => patchSettings({ auto_idle_s: parseFloat(idle) || 900 }), children: 'Save' })
      ] })
    ] }) : null,

    jsxs('div', { className: 'rounded-md border border-(--ui-stroke-secondary) p-3 flex flex-col gap-2', children: [
      jsx('span', { className: 'text-xs font-medium', children: 'Clients' }),
      jsx('div', { className: 'text-[0.625rem] text-(--ui-text-tertiary)', children:
        activeClient ? `Active: ${activeClient.name} · ${activeClient.folder}` : 'Loading…' }),
      jsx('div', { className: 'flex flex-col gap-1', children:
        clients.map((c) => jsxs('div', { className: 'flex items-center justify-between text-xs', children: [
          jsx('span', { className: c.active ? 'font-semibold text-(--ui-accent)' : '', children: c.name }),
          c.active ? null : jsx(Button, {
            className: 'h-6 px-2 text-[0.625rem]', variant: 'ghost',
            onClick: () => switchClient(ctx, c.id), children: 'Switch'
          })
        ], key: c.id }))
      }),
      jsxs('div', { className: 'flex items-center gap-2 pt-1', children: [
        jsx(Input, { className: 'h-7 flex-1 text-xs', placeholder: 'New client name…',
          value: newClient, onChange: (e) => setNewClient(e.target.value),
          onKeyDown: (e) => {
            if (e.key === 'Enter' && newClient.trim()) { addClient(ctx, newClient.trim()); setNewClient('') }
          } }),
        jsx(Button, { className: 'h-7', variant: 'ghost', disabled: !newClient.trim() || resting,
          onClick: () => { if (newClient.trim()) { addClient(ctx, newClient.trim()); setNewClient('') } },
          children: 'Add client' })
      ] })
    ] })
  ] })
}

// ── Main pane ─────────────────────────────────────────────────────────────────
function Pane({ ctx }) {
  const state = useValue($state)
  const connected = useValue($connected)
  const view = useValue($view)
  const error = useValue($stateError)

  useEffect(() => {
    const t = setInterval(() => $now.set(Date.now() / 1000), TICK_MS)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    refreshState(ctx)
    refreshClients(ctx)
    const iv = setInterval(() => refreshState(ctx), REST_MS)
    return () => clearInterval(iv)
  }, [ctx])

  useEffect(() => {
    const dispose = ctx.socket('/events', (msg) => {
      try {
        const m = msg || {}
        if (m.type === 'snapshot') { $state.set(m); $connected.set(true) }
        else if (m.type === 'ping' || m.type === 'auto_status') { $connected.set(true) }
        else if (m.type === 'client_activated') {
          Promise.all([refreshClients(ctx), refreshState(ctx)])
        }
        else if (['entry_created', 'entry_updated', 'entry_deleted', 'task_created',
                  'task_updated', 'task_deleted', 'settings_updated'].includes(m.type)) {
          refreshState(ctx)
        }
      } catch (e) { /* ignore malformed frames */ }
    })
    return dispose
  }, [ctx])

  const tabs = [
    { id: 'track', label: 'Track', icon: 'i-codicon-play' },
    { id: 'entries', label: 'Entries', icon: 'i-codicon-list-unordered' },
    { id: 'tasks', label: 'Tasks', icon: 'i-codicon-list-tree' },
    { id: 'invoice', label: 'Invoice', icon: 'i-codicon-graph' },
    { id: 'rate', label: 'Rate', icon: 'i-codicon-settings-gear' }
  ]

  return jsxs('div', { className: 'flex h-full flex-col gap-2 p-2 text-sm', children: [
    jsx(StyleTag, {}),
    jsx(Header, { ctx, state, connected }),
    error ? jsx('div', { className: 'rounded border border-(--ui-danger)/40 bg-(--ui-danger)/10 px-2 py-1 text-[0.6875rem] text-(--ui-danger)', children: error }) : null,
    jsxs('div', { className: 'flex gap-1', children:
      tabs.map((t) => jsx('button', {
        key: t.id,
        type: 'button',
        onClick: () => { $view.set(t.id); haptic('tap') },
        className: cn(
          'inline-flex h-7 flex-1 items-center justify-center gap-1 rounded-md px-1.5 text-[0.625rem] font-medium transition-all',
          view === t.id ? 'bg-(--ui-accent) text-(--ui-on-accent)' : 'text-(--ui-text-secondary) hover:bg-(--ui-bg-secondary)'
        ),
        children: [jsx('i', { className: t.icon }), t.label]
      }))
    }),
    jsx('div', { className: 'min-h-0 flex-1 overflow-y-auto', children:
      view === 'track' ? jsx(TimerPanel, { ctx, state })
      : view === 'entries' ? jsx(EntriesView, { ctx, state })
      : view === 'tasks' ? jsx(TasksView, { ctx, state })
      : view === 'invoice' ? jsx(InvoiceView, { ctx, state })
      : jsx(SettingsRow, { ctx, state })
    })
  ] })
}

export default {
  id: ID,
  name: 'POW Tracker',
  defaultEnabled: true,
  register(ctx) {
    ctx.registerMany([
      // Dockable right-edge pane, always one click away.
      {
        id: 'pane',
        area: PANES_AREA,
        title: 'POW Tracker',
        data: { placement: 'right', width: '320px' },
        render: () => jsx(Pane, { ctx })
      },
      // Full route so the sidebar entry has somewhere to land.
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/pow-tracker' },
        render: () => jsx(Pane, { ctx })
      },
      // Sidebar nav row, lights up at the route.
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 52,
        data: { path: '/pow-tracker', label: 'POW', codicon: 'clock' }
      },
      // Command-palette open.
      {
        id: 'open',
        area: PALETTE_AREA,
        title: 'POW Tracker: Open',
        data: { id: 'pow.open', run: () => { host.navigate('/pow-tracker') } }
      }
    ])
  }
}


// Self-register for Hermes dashboard
if (typeof window !== 'undefined' && window.__HERMES_PLUGINS__) {
  window.__HERMES_PLUGINS__.register('pow-tracker', default);
}
