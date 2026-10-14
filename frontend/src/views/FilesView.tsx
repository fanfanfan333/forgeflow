import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { ResourcePicker } from './runs/ResourcePicker'
import { ContextSyncStatus } from '../components/ContextSyncStatus'
import '../styles/skill-library.css'
import '../styles/runs.css'
import '../styles/files.css'

export function FilesView() {
  const [selected, setSelected] = useState<string[]>([])
  return <section className="skills-library-page files-page" data-testid="files-workbench-page"><header className="skills-library-heading"><div><h1>文件与数据</h1><p>把文件、数据或代码项目交给智能体，在工作区查看处理结果与变更。</p></div></header>
    <ResourcePicker workbench selectedIds={selected} onToggle={id=>setSelected(current=>current.includes(id) ? current.filter(value=>value!==id) : [id])} onRegistered={record=>setSelected([record.id])} />
    {!!selected.length && <div className="files-selection-actions"><Link className="skill-primary" to={'/files/use/'+encodeURIComponent(selected[0])}>让智能体处理这个对象 ↗</Link><button onClick={() => setSelected([])}>取消选择</button></div>}
    <ContextSyncStatus />
  </section>
}
