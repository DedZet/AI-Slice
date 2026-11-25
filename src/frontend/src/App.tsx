import { lazy, Suspense, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import './App.css'
const ModelPreview = lazy(() => import('./ModelPreview'))

type Profile = { id: string; printer: string; material: string }
type Status = 'queued' | 'running' | 'review' | 'slicing' | 'approved' | 'failed'
type ChatMessage = { role: 'user' | 'assistant'; content: string }
type Result = {
  revision?: number
  message: string
  mode: 'local' | 'api'
  risks: string[]
  geometry: { min_sampled_wall_mm: number | null }
  bridge: { max_sampled_bridge_mm: number | null }
  project: {
    printer?: string
    material?: string
    bed_type?: string | null
    orientation: { id: string; contact_mm2: number; overhang_mm2: number }
    variant: { id: string; infill_pct: number; supports: 'none' | 'tree' }
    settings?: {
      layer_height?: string | null
      wall_loops?: string | null
      sparse_infill_pattern?: string | null
      outer_wall_speed?: string | string[] | null
    }
  }
  slice_warning?: string
  metrics?: { estimated_seconds: number | null; filament_g: number | null; filament_mm: number | null }
}
type Job = {
  job_id: string
  profile_id: string
  revision: number
  history?: ChatMessage[]
  status: Status
  step?: string
  prompt: string
  error?: { code: string; message: string }
  result?: Result
  artifacts?: string[]
}

const stages: Record<string, string> = {
  inspect_model: 'Анализ сетки',
  analyze_orientations: 'Подбор ориентации',
  set_parameters: 'Выбор настроек печати',
  slice_model: 'Нарезка в OrcaSlicer',
  preview_model: 'Подготовка 3D-предпросмотра',
}

async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options)
  const body = await response.json()
  if (!response.ok) {
    const detail = body.detail
    throw new Error(typeof detail === 'string' ? detail : detail?.message ?? `Ошибка HTTP ${response.status}`)
  }
  return body as T
}

function Measure({ value, unit }: { value: number | null; unit: string }) {
  return value === null ? '—' : `${value.toLocaleString('ru-RU', { maximumFractionDigits: 2 })} ${unit}`
}

function ProfileOptions({ profiles }: { profiles: Profile[] }) {
  const printers = new Map<string, Profile[]>()
  for (const profile of profiles) {
    const group = printers.get(profile.printer)
    if (group) group.push(profile)
    else printers.set(profile.printer, [profile])
  }
  return Array.from(printers, ([printer, values]) => (
    <optgroup label={printer} key={printer}>
      {values.map(profile => <option key={profile.id} value={profile.id}>{profile.material} · {printer}</option>)}
    </optgroup>
  ))
}

function PrintReport({ result, jobId }: { result: Result; jobId: string }) {
  const { metrics, project, geometry } = result
  return (
    <section className="report" aria-label="Отчёт подготовки">
      <h2>{result.revision ? `План печати · версия ${result.revision}` : 'Результат подготовки'}</h2>
      {project.printer && <p>{project.printer} · {project.material}</p>}
      {result.mode === 'local' && <p>Локальный режим. Параметры выбраны по геометрии и распознанным пожеланиям.</p>}
      {result.message && <p>{result.message}</p>}
      {result.revision && <Suspense fallback={<p>Загрузка 3D-предпросмотра…</p>}>
        <ModelPreview key={result.revision} url={`/api/jobs/${jobId}/preview/${result.revision}.stl`} />
      </Suspense>}
      <dl className="metrics">
        {metrics && <><div><dt>Время печати</dt><dd><Measure value={metrics.estimated_seconds === null ? null : metrics.estimated_seconds / 60} unit="мин" /></dd></div>
        <div><dt>Расход материала</dt><dd>{metrics.filament_g === null
          ? <Measure value={metrics.filament_mm} unit="мм" />
          : <Measure value={metrics.filament_g} unit="г" />}</dd></div></>}
        {project.bed_type && <div><dt>Пластина</dt><dd>{project.bed_type}</dd></div>}
        {project.settings?.layer_height && <div><dt>Высота слоя</dt><dd>{project.settings.layer_height} мм</dd></div>}
        {project.settings?.wall_loops && <div><dt>Периметры</dt><dd>{project.settings.wall_loops}</dd></div>}
        <div><dt>Заполнение</dt><dd>{project.variant.infill_pct}%</dd></div>
        {project.settings?.sparse_infill_pattern && <div><dt>Рисунок заполнения</dt><dd>{project.settings.sparse_infill_pattern}</dd></div>}
        <div><dt>Поддержки</dt><dd>{project.variant.supports === 'tree' ? 'Древовидные' : 'Выключены'}</dd></div>
        <div><dt>Контакт со столом</dt><dd><Measure value={project.orientation.contact_mm2} unit="мм²" /></dd></div>
        <div><dt>Площадь нависаний</dt><dd><Measure value={project.orientation.overhang_mm2} unit="мм²" /></dd></div>
        <div><dt>Мин. толщина в выборке</dt><dd><Measure value={geometry.min_sampled_wall_mm} unit="мм" /></dd></div>
        {result.bridge.max_sampled_bridge_mm !== null && <div><dt>Пролёт моста в выборке</dt><dd><Measure value={result.bridge.max_sampled_bridge_mm} unit="мм" /></dd></div>}
      </dl>
      {result.slice_warning && <p>{result.slice_warning}</p>}
      {result.risks.length > 0 && <ul>{result.risks.map(risk => <li key={risk}>{risk}</li>)}</ul>}
    </section>
  )
}

export default function App() {
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [profileId, setProfileId] = useState('')
  const [job, setJob] = useState<Job>()
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [loadingProfiles, setLoadingProfiles] = useState(true)
  const live = job?.status === 'queued' || job?.status === 'running' || job?.status === 'slicing'
  const jobId = job?.job_id

  useEffect(() => {
    const request = new AbortController()
    api<Profile[]>('/api/profiles', { signal: request.signal }).then(items => {
      setProfiles(items)
      setProfileId(items[0]?.id ?? '')
    }).catch((err: Error) => {
      if (!request.signal.aborted) setError(err.message)
    }).finally(() => {
      if (!request.signal.aborted) setLoadingProfiles(false)
    })
    return () => request.abort()
  }, [])

  useEffect(() => {
    if (!live || !jobId) return
    const request = new AbortController()
    let timer: number
    async function refresh() {
      try {
        const current = await api<Job>(`/api/jobs/${jobId}`, { signal: request.signal })
        setJob(current)
        setError('')
        if (!['queued', 'running', 'slicing'].includes(current.status)) return
      } catch (err) {
        if (request.signal.aborted) return
        setError((err as Error).message)
      }
      if (!request.signal.aborted) timer = window.setTimeout(refresh, 1200)
    }
    timer = window.setTimeout(refresh, 500)
    return () => {
      request.abort()
      window.clearTimeout(timer)
    }
  }, [jobId, live])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setPending(true)
    setError('')
    try {
      setJob(await api<Job>('/api/jobs', { method: 'POST', body: form }))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setPending(false)
    }
  }

  async function update(action: 'approve' | 'revise', change?: { prompt: string; profile_id?: string }) {
    if (!job) return
    setPending(true)
    setError('')
    try {
      setJob(await api<Job>(`/api/jobs/${jobId}/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: job.revision, ...change }),
      }))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="workspace">
      <header><h1>AI-SLICE</h1><p>Подготовка модели к FDM-печати</p></header>
      {job && <section className="conversation" aria-label="Диалог">
        {job.history?.map((message, index) => (
          <p className={message.role === 'user' ? 'user-message' : 'assistant-message'} key={index}>{message.content}</p>
        ))}
        <p className="user-message">{job.prompt}</p>
        {live && <p role="status">{job.step ? stages[job.step] : 'Задача в очереди'}</p>}
        {job.error && <p role="alert">{job.error.message} ({job.error.code})</p>}
        {job.result && (job.result.revision === job.revision || job.status === 'approved') && <PrintReport result={job.result} jobId={job.job_id} />}
        {job.status === 'review' && job.result?.revision === job.revision && <button type="button" disabled={pending} onClick={() => void update('approve')}>Подтвердить и нарезать</button>}
        {((job.status === 'review' && job.result?.revision === job.revision) || (job.status === 'failed' && job.revision > 1)) && (
          <form className="revision-form" key={job.revision} onSubmit={event => {
            event.preventDefault()
            const form = new FormData(event.currentTarget)
            const prompt = String(form.get('revision_prompt') ?? '').trim()
            const profile_id = String(form.get('revision_profile') ?? '') || undefined
            if (prompt) void update('revise', { prompt, profile_id })
          }}>
            <label>Что изменить?
              <textarea name="revision_prompt" rows={2} maxLength={4000} required placeholder="Например, увеличь заполнение до 40%." />
            </label>
            <label>Профиль для новой версии
              <select name="revision_profile" defaultValue="">
                <option value="">Сохранить текущий профиль</option>
                <ProfileOptions profiles={profiles.filter(profile => profile.id !== job.profile_id)} />
              </select>
            </label>
            <button disabled={pending}>Отправить правки</button>
          </form>
        )}
        {job.artifacts && <div className="downloads">{job.artifacts.map(name =>
          <a key={name} href={`/api/jobs/${job.job_id}/artifacts/${name}`}>Скачать {name}</a>,
        )}</div>}
      </section>}
      {(!job || job.status === 'approved' || (job.status === 'failed' && job.revision === 1) || (job.status === 'review' && !job.result?.revision)) && <form onSubmit={submit} className="composer">
        <fieldset disabled={pending || live}>
          <label>Профиль принтера и материала
            <select name="profile_id" required value={profileId} onChange={event => setProfileId(event.target.value)}>
              {!profiles.length && <option value="">{loadingProfiles ? 'Загрузка профилей…' : 'Нет доступных профилей'}</option>}
              <ProfileOptions profiles={profiles} />
            </select>
          </label>
          <label>Модель STL в миллиметрах<input type="file" name="model" accept=".stl" required /></label>
          <label>Пожелания к печати
            <textarea name="prompt" rows={3} maxLength={4000} required
              placeholder="Например, нужна прочность вдоль оси X. Предпочтительное заполнение — 25%." />
          </label>
          <button disabled={!profileId}>Подготовить модель</button>
        </fieldset>
      </form>}
      {error && <p role="alert">{error}</p>}
    </main>
  )
}
