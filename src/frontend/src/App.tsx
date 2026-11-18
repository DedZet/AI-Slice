// frontend/src/App.tsx
import { useEffect, useState } from 'react'

function App() {
  const [message, setMessage] = useState('')

  useEffect(() => {
    fetch('/api/health')
      .then(res => res.json())
      .then(data => setMessage(data.status))
  }, [])

  return (
    <div>
      <h1>React + FastAPI</h1>
      <p>Статус бэкенда: {message || 'Загрузка...'}</p>
    </div>
  )
}

export default App