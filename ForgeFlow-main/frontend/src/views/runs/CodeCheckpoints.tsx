import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { request } from '../../api/client'
import { humanizeError } from '../../api/errors'
import { roleAtLeast } from '../../auth/roleGate'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { useSession } from '../../hooks/useSession'

type FileVersion = { file: string; stored_hash: string; current_hash: string | null; bytes: number }
type CheckpointList = { version: number; state: string; items: { version: number; file_count: number }[] }
type CheckpointFiles = { version: number; state: string; checkpoint_version: number; total: number; files: FileVersion[] }

export function CodeCheckpoints({ workspaceId }: { workspaceId: string }) {
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState<number | null>(null)
  const [offset, setOffset] = useState(0)
  const [pending, setPending] = useState<FileVersion | null>(null)
  const canRestore = roleAtLeast(useSession()?.role, 'manager')
  const cache = useQueryClient()
  const base = `/codeplane/workspaces/${encodeURIComponent(workspaceId)}`
  const prefix = ['codeplane', 'checkpoints', workspaceId]
  const checkpoints = useQuery({ queryKey: prefix, queryFn: () => request<CheckpointList>(`${base}/checkpoints`), enabled: open && !!workspaceId, retry: false })
  const version = selected ?? checkpoints.data?.items[0]?.version
  const files = useQuery({ queryKey: [...prefix, version, offset], queryFn: () => request<CheckpointFiles>(`${base}/checkpoints/${version}?offset=${offset}`), enabled: open && version !== undefined, retry: false })
  const restore = useMutation({ mutationFn: (file: FileVersion) => request(`${base}/restore-file`, {
    method: 'POST', body: JSON.stringify({ checkpoint_version: version, expected_checkpoint_version: files.data?.version, file: file.file, expected_current_hash: file.current_hash }),
  }), onSuccess: () => { setPending(null); void cache.invalidateQueries({ queryKey: prefix }) }, onError: () => { setPending(null); void cache.invalidateQueries({ queryKey: prefix }) } })
  if (!workspaceId) return null
  return <details className="code-checkpoints" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>文件检查点与恢复</summary>
    <p>恢复单个文件，并保留当前内容的备份。不会恢复任务执行，也不会回滚数据库或远程操作。</p>
    {checkpoints.isPending && open && <p>正在读取检查点…</p>}
    {(checkpoints.isError || files.isError) && <p role="alert">{humanizeError(checkpoints.error || files.error, '检查点读取失败').label} <button className="btn ghost sm" onClick={() => { void checkpoints.refetch(); void files.refetch() }}>重试</button></p>}
    {checkpoints.data && !checkpoints.data.items.length && <p>历史任务没有文件检查点，不能恢复。</p>}
    {!!checkpoints.data?.items.length && <label>检查点 <select aria-label="文件检查点版本" value={version} onChange={event => { setSelected(Number(event.target.value)); setOffset(0) }}>{checkpoints.data.items.map(item => <option key={item.version} value={item.version}>版本 {item.version} · {item.file_count} 个文件</option>)}</select></label>}
    {files.data && <><p>{files.data.state === 'released' ? '工作区已停止，可以选择文件恢复。' : '工作区仍在运行或状态未确认，暂不能恢复。'}</p><ul>{files.data.files.map(file => <li key={file.file}><span>{file.file}</span> <small>{file.stored_hash === file.current_hash ? '内容一致' : '内容已变化'}</small> {canRestore && file.stored_hash !== file.current_hash && <button className="btn ghost sm" disabled={files.data.state !== 'released' || restore.isPending} onClick={() => setPending(file)}>恢复此文件</button>}</li>)}</ul><div><button className="btn ghost sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</button> <span>{files.data.total ? offset + 1 : 0}–{Math.min(offset + 50, files.data.total)} / {files.data.total}</span> <button className="btn ghost sm" disabled={offset + 50 >= files.data.total} onClick={() => setOffset(offset + 50)}>下一页</button></div></>}
    {restore.isError && <p role="alert">{humanizeError(restore.error, '恢复失败，当前内容未被确认覆盖').label}</p>}
    <ConfirmDialog open={pending !== null} title="恢复文件？" body={`将 ${pending?.file || ''} 恢复到所选版本。当前内容会先备份；若文件再次变化，操作会被拒绝。恢复后需要重新检查和审批。`} confirmLabel="恢复文件" busy={restore.isPending} onConfirm={() => { if (pending) restore.mutate(pending) }} onCancel={() => setPending(null)} />
  </details>
}
