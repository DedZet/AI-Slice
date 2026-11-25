import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import './App.css'

type Profile = { id: string; printer: string; material: string }
type Status = 'queued' | 'running' | 'review' | 'approved' | 'failed'
type Result = {
  message: string
  mode: 'local' | 'api'
  risks: string[]
  geometry: { min_sampled_wall_mm: number | null }
  bridge: { max_sampled_bridge_mm: number | null }
  project: {
    orientation: { id: string; contact_mm2: number; overhang_mm2: number }
    variant: { infill_pct: number; supports: 'none' | 'tree' }
  }
  metrics: { estimated_seconds: number | null; filament_g: number | null; filament_mm: number | null }
}
type Job = {
  job_id: string
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
  check_result: 'Проверка результата',
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

function PrintReport({ result }: { result: Result }) {
  const { metrics, project, geometry } = result
  return (
    <section className="report" aria-label="Отчёт подготовки">
      <h2>Результат подготовки</h2>
      {result.mode === 'local' && <p>Локальный режим. Текст пожеланий сохранён, параметры выбраны по геометрии.</p>}
      {result.message && <p>{result.message}</p>}
      <dl className="metrics">
        <div><dt>Время печати</dt><dd><Measure value={metrics.estimated_seconds === null ? null : metrics.estimated_seconds / 60} unit="мин" /></dd></div>
        <div><dt>Расход материала</dt><dd>{metrics.filament_g === null
          ? <Measure value={metrics.filament_mm} unit="мм" />
          : <Measure value={metrics.filament_g} unit="г" />}</dd></div>
        <div><dt>Заполнение</dt><dd>{project.variant.infill_pct}%</dd></div>
        <div><dt>Поддержки</dt><dd>{project.variant.supports === 'tree' ? 'Древовидные' : 'Выключены'}</dd></div>
        <div><dt>Контакт со столом</dt><dd><Measure value={project.orientation.contact_mm2} unit="мм²" /></dd></div>
        <div><dt>Площадь нависаний</dt><dd><Measure value={project.orientation.overhang_mm2} unit="мм²" /></dd></div>
        <div><dt>Мин. толщина в выборке</dt><dd><Measure value={geometry.min_sampled_wall_mm} unit="мм" /></dd></div>
        {result.bridge.max_sampled_bridge_mm !== null && <div><dt>Пролёт моста в выборке</dt><dd><Measure value={result.bridge.max_sampled_bridge_mm} unit="мм" /></dd></div>}
      </dl>
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
  const live = job?.status === 'queued' || job?.status === 'running'
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
        if (current.status !== 'queued' && current.status !== 'running') return
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

  async function approve() {
    setPending(true)
    setError('')
    try {
      setJob(await api<Job>(`/api/jobs/${jobId}/approve`, { method: 'POST' }))
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
        <p className="user-message">{job.prompt}</p>
        {live && <p role="status">{job.step ? stages[job.step] : 'Задача в очереди'}</p>}
        {job.error && <p role="alert">{job.error.message} ({job.error.code})</p>}
        {job.result && <PrintReport result={job.result} />}
        {job.status === 'review' && <button type="button" disabled={pending} onClick={approve}>Принять результат</button>}
        {job.artifacts && <div className="downloads">{job.artifacts.map(name =>
          <a key={name} href={`/api/jobs/${job.job_id}/artifacts/${name}`}>Скачать {name}</a>,
        )}</div>}
      </section>}
      <form onSubmit={submit} className="composer">
        <fieldset disabled={pending || live}>
          <label>Профиль принтера и материала
            <select name="profile_id" required value={profileId} onChange={event => setProfileId(event.target.value)}>
              {!profiles.length && <option value="">{loadingProfiles ? 'Загрузка профилей…' : 'Нет доступных профилей'}</option>}
              {profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.printer} · {profile.material}</option>)}
            </select>
          </label>
          <label>Модель STL в миллиметрах<input type="file" name="model" accept=".stl" required /></label>
          <label>Пожелания к печати
            <textarea name="prompt" rows={3} maxLength={4000} required
              placeholder="Например, нужна прочность вдоль оси X. Предпочтительное заполнение — 25%." />
          </label>
          <button disabled={!profileId}>Подготовить модель</button>
        </fieldset>
      </form>
      {error && <p role="alert">{error}</p>}
    </main>
  )
}
