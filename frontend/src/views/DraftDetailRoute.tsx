/**
 * The draft detail route (`#/companies/{companyId}/drafts/{draftId}`): owns
 * the two reads behind the screen — the draft (`GET /drafts/{id}`) and the
 * actor's allowed actions (`GET /drafts/{id}/transitions`) — plus the three
 * writes: the transition POST, the signed-copy upload (the raw file to the
 * signature route) and the export (the mode to the exports route).
 *
 * Both reads are id-scoped: the company in the path is navigation context
 * (the links back, and the entry point from the company screen), not a read
 * parameter — the API learns the draft's company from the row and answers a
 * cross-tenant id with the same 404 as an unknown one. After a successful
 * write both reads are re-read: the status may have moved, so the actions the
 * API offers may have changed with it, and the version's document is what the
 * edit may have rewritten.
 *
 * The transition POST's own reply is handed back to the view unread by
 * anything below this route: the release's `signature_package` block lives on
 * that reply and nowhere else, so the screen that must show it is the one that
 * sent the action. The export's reply, the FilingPackage, is handed back the
 * same way.
 */

import {
  exportDraft,
  fetchDraft,
  fetchDraftTransitions,
  postDraftTransition,
  uploadSignedDraft,
} from '../api/endpoints'
import type { Draft, DraftTransitionRequest, ExportMode, FilingPackage } from '../api/types'
import { ErrorPanel } from '../components/ErrorPanel'
import { useMfaRefusalGuard } from '../mfa'
import { hrefFor, hrefForCompany } from '../route'
import { useResource } from '../useResource'
import { DraftDetailView } from './DraftDetailView'

export function DraftDetailRoute({
  companyId,
  draftId,
}: {
  companyId: string
  draftId: string
}) {
  const [draft, reloadDraft] = useResource(() => fetchDraft(draftId), `draft:${draftId}`)
  const [transitions, reloadTransitions] = useResource(
    () => fetchDraftTransitions(draftId),
    `draft-transitions:${draftId}`,
  )
  const firstRefusal =
    [draft, transitions].find((read) => read.status === 'error')?.error ?? null
  useMfaRefusalGuard(firstRefusal)

  if (draft.status === 'error') {
    return <LoadFailure companyId={companyId} error={draft.error} onRetry={reloadDraft} />
  }
  if (draft.data === null) {
    return <p className="panel muted">Loading the draft…</p>
  }
  if (transitions.status === 'error') {
    return (
      <LoadFailure companyId={companyId} error={transitions.error} onRetry={reloadTransitions} />
    )
  }
  if (transitions.data === null) {
    return <p className="panel muted">Loading the draft…</p>
  }

  async function act(request: DraftTransitionRequest): Promise<Draft> {
    const updated = await postDraftTransition(draftId, request)
    reloadDraft()
    reloadTransitions()
    return updated
  }

  /** The signed copy: the file itself rides the signature route; a success re-reads both lists. */
  async function upload(file: File): Promise<void> {
    await uploadSignedDraft(draftId, file)
    reloadDraft()
    reloadTransitions()
  }

  /** The export: the mode rides the exports route's body; the package reply is the view's to hold. */
  async function exportWithMode(mode: ExportMode): Promise<FilingPackage> {
    const pkg = await exportDraft(draftId, mode)
    reloadDraft()
    reloadTransitions()
    return pkg
  }

  return (
    <DraftDetailView
      draft={draft.data}
      transitions={transitions.data}
      onAct={act}
      onUpload={upload}
      onExport={exportWithMode}
      onRefresh={() => {
        reloadDraft()
        reloadTransitions()
      }}
    />
  )
}

function LoadFailure({
  companyId,
  error,
  onRetry,
}: {
  companyId: string
  error: unknown
  onRetry: () => void
}) {
  return (
    <div className="view">
      <p className="muted">
        <a href={hrefFor('dashboard')}>← Portfolio dashboard</a> ·{' '}
        <a href={hrefForCompany(companyId)}>← Company</a>
      </p>
      <ErrorPanel error={error} onRetry={onRetry} />
    </div>
  )
}
