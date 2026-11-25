import { useEffect, useRef, useState } from 'react'
import {
  AmbientLight, Box3, Color, DirectionalLight, GridHelper, Mesh, MeshStandardMaterial,
  PerspectiveCamera, Scene, Vector3, WebGLRenderer,
} from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { STLLoader } from 'three/addons/loaders/STLLoader.js'

type PreviewProps = {
  url: string
  bedOrigin?: [number, number]
  bedSize?: [number, number, number]
}

export default function ModelPreview({ url, bedOrigin, bedSize }: PreviewProps) {
  const host = useRef<HTMLDivElement>(null)
  const [error, setError] = useState('')
  const [ready, setReady] = useState(false)
  const [bedX, bedY] = bedOrigin ?? [0, 0]
  const [bedWidth, bedDepth] = bedSize ?? [0, 0]
  const hasBed = bedWidth > 0 && bedDepth > 0

  useEffect(() => {
    const element = host.current
    if (!element) return
    const controller = new AbortController()
    let renderer: WebGLRenderer
    try {
      renderer = new WebGLRenderer({ antialias: true })
    } catch {
      const timer = window.setTimeout(() => setError('Для просмотра модели нужен браузер с WebGL.'))
      return () => window.clearTimeout(timer)
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.domElement.setAttribute('aria-label', 'Вращаемая 3D-модель')
    element.append(renderer.domElement)
    const scene = new Scene()
    scene.background = new Color(0xe8f0eb)
    scene.add(new AmbientLight(0xffffff, 1.7))
    const light = new DirectionalLight(0xffffff, 2.4)
    light.position.set(4, -5, 8)
    scene.add(light)
    const camera = new PerspectiveCamera(40, 1, 0.01, 200)
    camera.up.set(0, 0, 1)
    camera.position.set(2.1, -2.8, 1.9)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.minDistance = hasBed ? .03 : .4
    controls.maxDistance = 10
    controls.target.set(0, 0, .35)
    controls.update()
    const grid = new GridHelper(hasBed ? 1 : 3, 18, 0x719486, 0xa9c0b4)
    grid.rotation.x = Math.PI / 2
    grid.position.z = -.003
    scene.add(grid)
    const material = new MeshStandardMaterial({ color: 0x287561, roughness: .75 })
    let mesh: Mesh | undefined
    const observer = new ResizeObserver(() => {
      const { clientWidth, clientHeight } = element
      if (!clientWidth || !clientHeight) return
      renderer.setSize(clientWidth, clientHeight)
      camera.aspect = clientWidth / clientHeight
      camera.updateProjectionMatrix()
    })
    observer.observe(element)
    renderer.setAnimationLoop(() => {
      controls.update()
      renderer.render(scene, camera)
    })

    fetch(url, { signal: controller.signal }).then(response => {
      if (!response.ok) throw new Error('Предпросмотр недоступен.')
      return response.arrayBuffer()
    }).then(buffer => {
      if (controller.signal.aborted) return
      const geometry = new STLLoader().parse(buffer)
      geometry.computeBoundingBox()
      const box = geometry.boundingBox ?? new Box3()
      const center = box.getCenter(new Vector3())
      const size = box.getSize(new Vector3())
      const scale = 1 / Math.max(size.x, size.y, size.z, bedWidth, bedDepth, .001)
      if (hasBed) {
        center.x = bedX + bedWidth / 2
        center.y = bedY + bedDepth / 2
        grid.scale.set(bedWidth * scale, 1, bedDepth * scale)
        camera.position.set(1.35, -1.8, 1.55)
      }
      geometry.translate(-center.x, -center.y, -box.min.z)
      geometry.scale(scale, scale, scale)
      mesh = new Mesh(geometry, material)
      scene.add(mesh)
      controls.target.z = size.z * scale / 2
      controls.update()
      setReady(true)
    }).catch(() => {
      if (!controller.signal.aborted) setError('Не удалось загрузить предпросмотр.')
    })
    return () => {
      controller.abort()
      observer.disconnect()
      renderer.setAnimationLoop(null)
      controls.dispose()
      grid.dispose()
      mesh?.geometry.dispose()
      material.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [url, bedX, bedY, bedWidth, bedDepth, hasBed])

  return (
    <figure className="preview">
      <div ref={host} className="preview-canvas" />
      <figcaption>
        {error || (ready ? 'Перетаскивайте, чтобы вращать, и прокручивайте, чтобы приблизить. Показано размещение на столе до построения поддержек и траекторий.' : 'Загрузка 3D-модели…')}
      </figcaption>
    </figure>
  )
}
