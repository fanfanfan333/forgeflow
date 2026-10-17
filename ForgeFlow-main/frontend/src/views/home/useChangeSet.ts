/**
 * useChangeSet — Phase C「Change Set」的数据 + 动作 hook（AUDIT §16.3 / §16.6 / §16.13）。
 *
 * 职责：把**既有**数据与**既有**动作接起来，**不新增任何端点 / mutation 层**：
 *   · 数据（代码面）：`useRunDetail(runId)`（`GET /runs/{id}`）→ `changeSet.ts::deriveChangeSet`
 *     纯派生；diff 行复用 `realRun.ts::deriveCodeDiff`（一事实一取数）。
 *   · 数据（文档面）：`useArtifactVersionChain(<文档 artifact content_ref>)`
 *     （`GET /artifacts/{id}/versions`，**复用既有端点**）→ 传入 `deriveChangeSet` 归一。
 *   · 动作：**复用**既有端点 ——
 *       代码面：`useCodeDecision`（`POST /codeplane/runs/{id}/approve|reject`）+ `useReplanRun`
 *         （`POST /runs/{id}/replan`）；
 *       文档面：`POST /artifacts/{id}/versions/{v}/approve|reject` + `POST /artifacts/{id}/revert`。
 *     三动作（应用 / 拒绝 / 继续修改）按 `source` 落到**对应**既有端点，**不**另造第二套请求。
 *
 * 诚实：无 `runId` / 无证据 ⇒ `changeSet=null`（UI 显诚实空态）；动作失败**不吞**，
 * 经 `humanizeError` 原样上抛给 UI 呈现（含「无待批准版本」等真实前置条件）。
 */
import { useMemo } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { ArtifactVersionRecord, RunDetail, VersionChainResponse } from '../../api/client'
import {
  approveArtifactVersion,
  rejectArtifactVersion,
  revertArtifactVersion,
} from '../../api/client'
import { humanizeError } from '../../api/errors'
import {
  useArtifactVersionChain,
  useCodeDecision,
  useReplanRun,
  useRunDetail,
} from '../../api/hooks'
import { deriveCodeDiff } from '../runs/realRun'
import type { CodeDiff } from '../runs/types'
import { deriveChangeSet, documentArtifactId } from './changeSet'
import type { ChangeSet, ChangeSetSource } from './changeSet'

/** 当前进行中的动作（`null` = 空闲）。文档面「继续修改」映射为 `'replan'`。 */
export type ChangeSetPendingAction = 'approve' | 'reject' | 'replan' | null

export type ChangeSetController = {
  /** 归一后的变更集（无证据 / 无 run ⇒ `null`，UI 显诚实空态）。 */
  changeSet: ChangeSet | null
  /** 原始运行详情（`null` = 未取到）。 */
  detail: RunDetail | null
  /** 供抽屉渲染逐文件 diff 行的**唯一真源**（`realRun.deriveCodeDiff`；文档面无 unified）。 */
  diff: CodeDiff | null
  /** 来源面（`null` = 无变更集）。 */
  source: ChangeSetSource | null
  /** 是否有可呈现的变更集。 */
  hasData: boolean
  /** 详情是否加载中。 */
  loading: boolean
  /** 详情取数错误（诚实呈现，非空即错误）。 */
  error: string | null
  /** 当前进行中的动作。 */
  pending: ChangeSetPendingAction
  /** 动作错误（诚实呈现，非空即错误）。 */
  actionError: string | null
  /** [应用修改] —— 代码面复用 `codeApprove`；文档面复用 artifact approve。 */
  apply: () => void
  /** [拒绝] —— 代码面复用 `codeReject`；文档面复用 artifact reject。 */
  reject: () => void
  /** [继续修改] —— 代码面复用 `replanRun`；文档面复用 `revert`（以已提交头为新基线）。 */
  reanalyze: () => void
}

/** 链上版本号最大的版本（无版本 ⇒ `null`）。 */
function latestVersion(chain: VersionChainResponse | null): ArtifactVersionRecord | null {
  const versions = chain?.versions ?? []
  let latest: ArtifactVersionRecord | null = null
  for (const v of versions) {
    if (latest === null || v.version > latest.version) latest = v
  }
  return latest
}

/** 链上最新的 `pending` 版本号（无 ⇒ `null`）。 */
function pendingVersionNumber(chain: VersionChainResponse | null): number | null {
  const pendings = (chain?.versions ?? []).filter((v) => v.state === 'pending')
  if (pendings.length === 0) return null
  return pendings.reduce((acc, v) => (v.version > acc ? v.version : acc), pendings[0].version)
}

export function useChangeSet(runId: string | null): ChangeSetController {
  const qc = useQueryClient()
  const detailQ = useRunDetail(runId)
  const detail = detailQ.data ?? null

  // 文档面的版本链归属（= run 文档 artifact 的 content_ref，Q-C2）；代码面 / 无产物 ⇒ null
  // ⇒ 该 query `enabled:false`（代码面零额外请求）。
  const artifactId = useMemo(() => documentArtifactId(detail), [detail])
  const chainQ = useArtifactVersionChain(artifactId)
  const chain = chainQ.data ?? null

  const decision = useCodeDecision()
  const replan = useReplanRun()
  const docAction = useMutation({
    mutationFn: (action: 'approve' | 'reject' | 'revert') => {
      if (!artifactId) {
        throw new Error('本次无文档制品，无法执行文档面动作（缺少版本链归属）')
      }
      if (action === 'revert') {
        const head = latestVersion(chain)
        const committed =
          head && head.state === 'committed' ? head.version : (chain?.head_version ?? null)
        if (committed === null || committed === undefined) {
          throw new Error('本次无已提交版本，无法继续修改')
        }
        return revertArtifactVersion(artifactId, committed, { run_id: runId ?? undefined })
      }
      const version = pendingVersionNumber(chain)
      if (version === null) {
        throw new Error(action === 'approve' ? '本次无待批准的文档版本' : '本次无待拒绝的文档版本')
      }
      return action === 'approve'
        ? approveArtifactVersion(artifactId, version)
        : rejectArtifactVersion(artifactId, version)
    },
    onSuccess: () => {
      // 版本链是文档面的真源 ⇒ 成功后失效链 + 运行详情（approve 会改变 state）。
      qc.invalidateQueries({ queryKey: ['artifacts', 'versions'] })
      qc.invalidateQueries()
    },
  })

  const changeSet = useMemo(() => deriveChangeSet(detail, chain), [detail, chain])
  const diff = useMemo(() => (detail ? deriveCodeDiff(detail) : null), [detail])

  const pending: ChangeSetPendingAction = decision.isPending
    ? (decision.variables?.action ?? null)
    : replan.isPending
      ? 'replan'
      : docAction.isPending
        ? docAction.variables === 'approve'
          ? 'approve'
          : docAction.variables === 'reject'
            ? 'reject'
            : 'replan'
        : null

  const actionErrorRaw = decision.error ?? replan.error ?? docAction.error
  const actionError = actionErrorRaw ? humanizeError(actionErrorRaw, '变更集动作失败').label : null
  const error = detailQ.error ? humanizeError(detailQ.error, '加载运行详情失败').label : null

  const target = runId
  const source = changeSet?.source ?? null
  return {
    changeSet,
    detail,
    diff,
    source,
    hasData: changeSet !== null,
    loading: detailQ.isLoading,
    error,
    pending,
    actionError,
    apply: () => {
      if (!target) return
      if (source === 'document') docAction.mutate('approve')
      else decision.mutate({ runId: target, action: 'approve' })
    },
    reject: () => {
      if (!target) return
      if (source === 'document') docAction.mutate('reject')
      else decision.mutate({ runId: target, action: 'reject' })
    },
    reanalyze: () => {
      if (!target) return
      if (source === 'document') docAction.mutate('revert')
      else replan.mutate({ runId: target })
    },
  }
}
