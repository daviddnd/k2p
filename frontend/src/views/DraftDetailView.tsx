/**
 * The draft detail (`#/companies/{companyId}/drafts/{draftId}`): the current
 * version in full — its QC flags, the values it resolved, the per-item
 * provenance with the changed items marked, its filing metadata and the
 * lineage's earlier versions — and **the actions this actor may perform now,
 * rendered from the API's transitions read and nothing else.**
 *
 * The backend is the sole authority on allowed actions: this view holds no
 * §4.2 rule of its own, maps `transitions.actions` into buttons in the read's
 * order, and lets the API's own codes come back verbatim when a stale screen
 * submits one the draft no longer admits. The actions with input are
 * `edit` (the filing metadata), `upload_signed` (the signed file),
 * `approve` (the statement confirmation, when one stands unconfirmed) and
 * `export` (the mode) — a resolved value comes from master data (or a member
 * record) and is picked up by regenerating, as the header says, so none of
 * their forms edits one.
 *
 * `send_for_signature` is the release: its reply carries the
 * `signature_package` block (the stored document and the fill's escalations),
 * which this screen renders from that reply alone — a re-read does not carry
 * it, so it is held in state and named as the release's own answer.
 *
 * Three more actions carry their own input, exactly the `edit` shape:
 * `upload_signed` opens a file picker that posts the signed PDF itself to the
 * signature route (the transitions door refuses that action — its performer
 * is the dedicated route); `approve` on a draft making the unconfirmed
 * item-16 statement asks the approver to tick the confirmation before the
 * press, posting `confirm_statement: true` (ruling R2 — a draft without a
 * statement approves plainly); `export` opens the mode chooser and calls the
 * exports route directly with the chosen mode (the transitions door has no
 * mode — ruling R4). The export's reply, the FilingPackage, is held in state
 * like the release's block and shown by `FilingPackagePanel`.
 *
 * Everything rendered below the header is a read of the version's own
 * documents: `resolved_state` through `src/drafts.ts`' readers,
 * `field_provenance` as its rows with `changed: true` marked (that is §2.9
 * step 8's "traceable to source" on screen), and the flags the generation
 * raised. Nothing is estimated here.
 */

import { useState } from 'react'
import type {
  AllowedDraftAction,
  Draft,
  DraftAction,
  DraftTransitionRequest,
  DraftTransitions,
  ExportMode,
  FilingPackage,
  SignaturePackage,
} from '../api/types'
import { RefusalNotice } from '../components/RefusalNotice'
import { FilingPackagePanel } from '../components/FilingPackagePanel'
import { SignaturePackagePanel } from '../components/SignaturePackagePanel'
import { FilingMetadataForm, FilingMetadataSummary } from '../components/FilingMetadataForm'
import { DraftStatusBadge } from '../components/badges'
import { changeEventTypeLabel, payloadRows } from '../change_events'
import {
  EXPORT_MODES,
  companyFactRows,
  draftActionLabel,
  draftActionNote,
  draftEditRequest,
  exportModeDescription,
  exportModeLabel,
  filingMetadataFormFrom,
  formVersionText,
  provenanceRows,
  qcFlagRows,
  resolvedChanges,
  resolvedCompanyName,
  resolvedMembers,
  resolvedOfficers,
  shareCapital,
  shareClassRows,
  signatureStatusLabel,
  sourceLabel,
  statementConfirmationPending,
  type FilingMetadataFormState,
} from '../drafts'
import { formatDate, formatDateTime, formLabel, shortId } from '../format'
import { hrefForCompany } from '../route'

export function DraftDetailView({
  draft,
  transitions,
  onAct,
  onUpload,
  onExport,
  onRefresh,
}: {
  draft: Draft
  transitions: DraftTransitions
  onAct: (request: DraftTransitionRequest) => Promise<Draft | void>
  /** The signed-copy upload: the file itself rides the signature route. */
  onUpload: (file: File) => Promise<void>
  /** The export: the chosen mode rides the exports route's body. */
  onExport: (mode: ExportMode) => Promise<FilingPackage>
  onRefresh: () => void
}) {
  const [openAction, setOpenAction] = useState<DraftAction | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  // The release's own reply: the package `send_for_signature` just produced.
  // It is not on the draft the reads return, so it lives here for this screen.
  const [released, setReleased] = useState<SignaturePackage | null>(null)
  // The export's own reply: the FilingPackage the exports route just stored.
  // It is that route's answer alone, so it lives here for this screen.
  const [exported, setExported] = useState<FilingPackage | null>(null)

  async function perform(request: DraftTransitionRequest): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      const updated = await onAct(request)
      setReleased(updated?.signature_package ?? null)
      setOpenAction(null)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  /** The edit path: the form owns the refusal it meets, so it is not caught here. */
  async function performEdit(form: FilingMetadataFormState): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await onAct(draftEditRequest(draft, form))
      setReleased(null)
      setOpenAction(null)
    } finally {
      setBusy(false)
    }
  }

  /** The upload path: the signature route answers with the moved draft, which the route re-reads. */
  async function performUpload(file: File): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await onUpload(file)
      setReleased(null)
      setOpenAction(null)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  /** The export path: the exports route answers with the FilingPackage, held here like the release's block. */
  async function performExport(mode: ExportMode): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      const pkg = await onExport(mode)
      setReleased(null)
      setExported(pkg)
      setOpenAction(null)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  /**
   * The actions that collect something before they post — the `edit`
   * precedent. `approve` joins the set only while the item-16 statement
   * stands unconfirmed (ruling R2); a draft without one approves plainly.
   */
  function needsInput(allowed: AllowedDraftAction): boolean {
    if (allowed.action === 'edit') return true
    if (allowed.action === 'upload_signed') return true
    if (allowed.action === 'export') return true
    if (allowed.action === 'approve') return statementConfirmationPending(draft)
    return allowed.rationale_required
  }

  function press(allowed: AllowedDraftAction): void {
    setError(null)
    if (needsInput(allowed)) {
      setOpenAction(allowed.action)
      return
    }
    void perform({ action: allowed.action })
  }

  const state = draft.resolved_state
  const company = resolvedCompanyName(state) ?? `Company ${shortId(draft.company_id)}`
  const facts = companyFactRows(state)
  const capital = shareCapital(state)
  const officers = resolvedOfficers(state)
  const members = resolvedMembers(state)
  const changes = resolvedChanges(state)
  const flags = qcFlagRows(draft.qc_flags)
  const provenance = provenanceRows(draft.field_provenance)
  const madeUpDate = readMadeUpDate(draft.filing_metadata)

  return (
    <div className="view">
      <p className="muted">
        <a href={hrefForCompany(draft.company_id)}>← {company}</a>
      </p>

      <section className="panel">
        <header className="panel-head">
          <div>
            <h1>
              {formLabel(draft.form_type)} <DraftStatusBadge status={draft.status} />
            </h1>
            <p className="muted">
              version {draft.version_no} · target date {formatDate(draft.target_date)} ·
              resolution as of that date · built against {formVersionText(draft.form_version)} ·
              generated {formatDateTime(draft.created_at)}
              {draft.created_by === null
                ? ' by the system'
                : ` by staff ${shortId(draft.created_by)}`}
            </p>
          </div>
          <button type="button" className="secondary" onClick={onRefresh}>
            Refresh
          </button>
        </header>
        <p className="muted">
          Resolved from this company&rsquo;s master data and the approved change events whose
          effective dates had arrived by the target date. <strong>The values below are not edited
          here</strong>: an edit states the filing metadata, and a value that is wrong comes from
          the underlying record — the officers and members panels on the company screen, or the
          company&rsquo;s master data — and is picked up by regenerating.
        </p>
        <table className="table">
          <tbody>
            <tr>
              <th scope="row">Signature</th>
              <td>{signatureStatusLabel(draft.signature_status)}</td>
            </tr>
            <tr>
              <th scope="row">Applied change events</th>
              <td>
                {draft.applied_event_ids.length === 0
                  ? 'None — the draft resolves master data alone'
                  : `${draft.applied_event_ids.length} linked (${draft.applied_event_ids
                      .map((id) => shortId(id))
                      .join(', ')})`}
              </td>
            </tr>
            <tr>
              <th scope="row">Versions before this one</th>
              <td>
                {draft.history.length === 0
                  ? 'None — this is the draft’s first version'
                  : draft.history
                      .map((entry) => `v${entry.version_no} · ${formatDateTime(entry.created_at)}`)
                      .join(', ')}
              </td>
            </tr>
            {draft.invalidated_at !== null && (
              <tr>
                <th scope="row">Invalidated</th>
                <td>
                  {formatDateTime(draft.invalidated_at)} — {draft.invalidation_reason ?? 'no reason recorded'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {released !== null && <SignaturePackagePanel package={released} />}
      {exported !== null && <FilingPackagePanel package={exported} />}

      <section className="panel">
        <h2>QC flags</h2>
        <p className="muted">
          What the generation found (§2.9 steps 5–6): a critical field with no source, an approved
          event it could not apply, or a difference against the prior filing no applied event
          explains. They are the generation&rsquo;s own read — an edit does not re-derive them, and
          regenerating does.
        </p>
        {flags.length === 0 ? (
          <p className="muted">The generation raised no QC flags.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Flag</th>
                <th scope="col">What it says</th>
              </tr>
            </thead>
            <tbody>
              {flags.map((flag, index) => (
                <tr key={`${flag.kind}-${index}`}>
                  <td>
                    <span className="company-name">{flag.label}</span>
                    <span className="brn">{flag.kind}</span>
                  </td>
                  <td>{flag.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="panel">
        <h2>Resolved values</h2>
        <p className="muted">
          The state the draft resolved, structured as the registry reads it. Every value is the
          record&rsquo;s own; where a value came from is in the provenance below.
        </p>

        <h3>Company</h3>
        {facts.length === 0 ? (
          <p className="muted">No company fact resolved on this version.</p>
        ) : (
          <table className="table">
            <tbody>
              {facts.map((row) => (
                <tr key={row.path}>
                  <th scope="row">{row.label}</th>
                  <td>
                    {row.value}
                    <span className="brn">
                      <code>{row.path}</code>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <h3>Share capital</h3>
        {capital.classes.length === 0 ? (
          <p className="muted">No share class resolved on this version.</p>
        ) : (
          <>
            {capital.classes.map((entry, index) => (
              <table className="table" key={index}>
                <tbody>
                  {shareClassRows(entry).map((row) => (
                    <tr key={row.label}>
                      <th scope="row">{row.label}</th>
                      <td>{row.value}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ))}
            {capital.total.length > 0 && (
              <p className="muted">
                Total (derived, never stored):{' '}
                {capital.total.map((row) => `${row.label} ${row.value}`).join(' · ')}
              </p>
            )}
          </>
        )}

        <h3>Officers</h3>
        {officers.length === 0 ? (
          <p className="muted">
            No officer resolved — the company has no appointment in office on the target date.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Officer</th>
                <th scope="col">Office</th>
                <th scope="col">Window</th>
                <th scope="col">Address</th>
                <th scope="col">Source</th>
              </tr>
            </thead>
            <tbody>
              {officers.map((officer, index) => (
                <tr key={index}>
                  <td>{officer.name}</td>
                  <td>
                    <span className="company-name">{officer.role}</span>
                    <span className="brn">
                      {officer.capacity.length === 0
                        ? 'no capacity boxes'
                        : officer.capacity.join(', ')}
                    </span>
                    {officer.alternateTo !== null && (
                      <span className="brn">alternate to {officer.alternateTo}</span>
                    )}
                  </td>
                  <td>
                    {officer.validFrom}
                    {officer.validTo === null ? ' — open-ended' : ` to ${officer.validTo}`}
                  </td>
                  <td>
                    {officer.correspondence !== '—' && (
                      <span className="brn">Correspondence: {officer.correspondence}</span>
                    )}
                    {officer.registeredOffice !== '—' && (
                      <span className="brn">Registered office: {officer.registeredOffice}</span>
                    )}
                    {officer.correspondence === '—' && officer.registeredOffice === '—' && '—'}
                  </td>
                  <td>{officer.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <h3>Members</h3>
        {members.length === 0 ? (
          <p className="muted">
            No member resolved — the company has no holding in effect on the target date. A member
            position is master data: there is no shareholding change event in v1.0, so a transfer
            is recorded on the record and picked up by regenerating.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Member</th>
                <th scope="col">Class and shares</th>
                <th scope="col">Amounts</th>
                <th scope="col">Window</th>
                <th scope="col">Source</th>
              </tr>
            </thead>
            <tbody>
              {members.map((member, index) => (
                <tr key={index}>
                  <td>
                    {member.name}
                    {member.address !== '—' && <span className="brn">{member.address}</span>}
                    {member.remarks !== null && <span className="brn">{member.remarks}</span>}
                  </td>
                  <td>
                    <span className="company-name">
                      {member.sharesHeld} × {member.className}
                    </span>
                    <span className="brn">
                      {member.percentageHeld === null ? '' : `${member.percentageHeld}%`}
                      {member.jointlyHeld === null ? '' : ` · jointly held: ${member.jointlyHeld}`}
                      {member.allotmentDate === null ? '' : ` · allotted ${member.allotmentDate}`}
                    </span>
                  </td>
                  <td>
                    <span className="brn">
                      paid {member.amountPaid ?? '—'} · unpaid {member.amountUnpaid ?? '—'}
                    </span>
                  </td>
                  <td>
                    {member.validFrom}
                    {member.validTo === null ? ' — open-ended' : ` to ${member.validTo}`}
                  </td>
                  <td>{member.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <h3>Applied changes</h3>
        {changes.length === 0 ? (
          <p className="muted">
            No change event entered this draft: every value is master data as of the target date.
          </p>
        ) : (
          <>
            {changes.map((change) => (
              <div key={change.eventId}>
                <p>
                  <span className="company-name">{changeEventTypeLabel(change.type)}</span>{' '}
                  <span className="brn">
                    effective {formatDate(change.effectiveDate)} · event {shortId(change.eventId)}
                  </span>
                </p>
                <table className="table">
                  <tbody>
                    {payloadRows(change.payload).map((row) => (
                      <tr key={row.path}>
                        <th scope="row">{row.label}</th>
                        <td>
                          {row.value}
                          <span className="brn">
                            <code>{row.path}</code>
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </>
        )}
      </section>

      <section className="panel">
        <h2>Provenance and change highlighting</h2>
        <p className="muted">
          One row per registry item (§2.9 step 8): the box it fills, the source the value resolved
          from, and whether the value <strong>changed</strong> against the prior filing. The flag
          is that comparison alone: an item an applied event placed reads unchanged when the prior
          filing stated the same, so unchanged never means the event went unapplied. A row with no
          source is an item nothing resolved — the same thing a QC flag may name.
        </p>
        {provenance.length === 0 ? (
          <p className="muted">No provenance is on this version.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Item</th>
                <th scope="col">Targets</th>
                <th scope="col">Source</th>
                <th scope="col">Changed</th>
              </tr>
            </thead>
            <tbody>
              {provenance.map((row) => (
                <tr key={row.item} className={row.changed ? 'changed' : undefined}>
                  <td>
                    <code>{row.item}</code>
                  </td>
                  <td>
                    <code>{row.target}</code>
                  </td>
                  <td>{sourceLabel(row.sourceType, row.sourceId)}</td>
                  <td>
                    {row.changed ? (
                      <span className="badge changed">Changed</span>
                    ) : (
                      <span className="muted">unchanged</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="panel">
        <h2>Filing metadata</h2>
        <p className="muted">
          The return&rsquo;s period, the member-particulars delivery channel, the presentor, the
          signature and the consent — what an <code>edit</code> states. The document is the
          draft&rsquo;s own; it is not resolved from master data.
        </p>
        <FilingMetadataSummary metadata={draft.filing_metadata} />
      </section>

      <section className="panel">
        <h2>What you may do now</h2>
        <p className="muted">
          The API&rsquo;s own list for this account and this draft&rsquo;s status — rendered here,
          never decided here: the backend is the sole authority on allowed actions. An action that
          is not here is not offered — it may need a permission this account does not hold, a step
          the status does not admit, or a piece of work that is not built yet (an unwired action
          would answer a named 409). After every action the page re-reads the draft.
        </p>

        {transitions.actions.length === 0 ? (
          <p className="muted">
            No action is available to this account on this draft now. An exported or invalidated
            draft is an end, and a system row is nobody&rsquo;s to press — the API never offers
            either.
          </p>
        ) : (
          <ul className="plain-list">
            {transitions.actions.map((allowed) => (
              <li key={allowed.action}>
                {openAction === allowed.action && allowed.action === 'edit' ? (
                  <FilingMetadataForm
                    initial={filingMetadataFormFrom(draft)}
                    madeUpDate={madeUpDate}
                    showStatement={draft.form_type === 'nar1'}
                    submitLabel="Save the filing metadata"
                    note="The edit posts only the filing-metadata fields this form changes; the resolved values and the provenance are untouched. Regenerating is what re-derives them."
                    onSubmit={performEdit}
                    onCancel={() => setOpenAction(null)}
                  />
                ) : openAction === allowed.action && allowed.action === 'upload_signed' ? (
                  <SignedUploadForm
                    busy={busy}
                    onSubmit={performUpload}
                    onCancel={() => setOpenAction(null)}
                  />
                ) : openAction === allowed.action &&
                  allowed.action === 'approve' &&
                  statementConfirmationPending(draft) ? (
                  <StatementApproveForm
                    busy={busy}
                    onSubmit={() => perform({ action: 'approve', confirm_statement: true })}
                    onCancel={() => setOpenAction(null)}
                  />
                ) : openAction === allowed.action && allowed.action === 'export' ? (
                  <ExportModeForm
                    busy={busy}
                    onSubmit={performExport}
                    onCancel={() => setOpenAction(null)}
                  />
                ) : openAction === allowed.action && allowed.rationale_required ? (
                  <RationaleForm
                    allowed={allowed}
                    busy={busy}
                    onSubmit={(rationale) =>
                      perform({ action: allowed.action, rationale })
                    }
                    onCancel={() => setOpenAction(null)}
                  />
                ) : (
                  <>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => press(allowed)}
                    >
                      {draftActionLabel(allowed.action)}
                      {needsInput(allowed) ? '…' : ''}
                    </button>{' '}
                    <span className="muted">
                      {draftActionNote(allowed.action) ?? 'the API lists this action for this account now'}
                      {allowed.rationale_required && ' A reason will be required.'}
                    </span>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}

        {error !== null && <RefusalNotice error={error} />}
      </section>

      {draft.history.length > 0 && (
        <section className="panel">
          <h2>Version history</h2>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Version</th>
                <th scope="col">Status</th>
                <th scope="col">Recorded</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>
                  v{draft.version_no}
                  <span className="muted"> (current)</span>
                </td>
                <td>
                  <DraftStatusBadge status={draft.status} />
                </td>
                <td>{formatDateTime(draft.created_at)}</td>
              </tr>
              {draft.history.map((entry) => (
                <tr key={entry.version_no}>
                  <td>v{entry.version_no}</td>
                  <td>
                    <DraftStatusBadge status={entry.status} />
                  </td>
                  <td>
                    {formatDateTime(entry.created_at)}
                    {entry.created_by !== null && (
                      <span className="brn">by staff {shortId(entry.created_by)}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted">
            Newest first. A version is never edited in place: regeneration appends a row, and an
            edit updates the current one. An earlier version&rsquo;s document is not re-read here —
            the current version is the draft.
          </p>
        </section>
      )}
    </div>
  )
}

/** The return's made-up date, as the version's own document states it. */
function readMadeUpDate(metadata: Record<string, unknown>): string | null {
  const returned = metadata.return
  if (typeof returned !== 'object' || returned === null) return null
  const value = (returned as Record<string, unknown>).made_up_date
  return typeof value === 'string' ? value : null
}

function RationaleForm({
  allowed,
  busy,
  onSubmit,
  onCancel,
}: {
  allowed: AllowedDraftAction
  busy: boolean
  onSubmit: (rationale: string) => Promise<void>
  onCancel: () => void
}) {
  const [rationale, setRationale] = useState('')
  return (
    <form
      className="decision-form"
      onSubmit={(event) => {
        event.preventDefault()
        void onSubmit(rationale.trim())
      }}
    >
      <label>
        Reason — required
        <textarea
          rows={2}
          maxLength={500}
          required
          value={rationale}
          onChange={(event) => setRationale(event.target.value)}
        />
      </label>
      <p className="muted">
        {draftActionLabel(allowed.action)} needs a reason — the API refuses it without one, and what
        you write lands on the draft and in the audit chain.
      </p>
      <span className="row-actions">
        <button type="submit" disabled={busy}>
          {busy ? 'Working…' : draftActionLabel(allowed.action)}
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </span>
    </form>
  )
}

/**
 * The signed-copy upload (`upload_signed`): a file picker, not a bare button —
 * the chosen file itself is the body of the signature route, posted raw with
 * its own content type. The API checks the declared type against the bytes,
 * and the stored WORM document and the draft's move to `signature_received`
 * commit or roll back together. The button stays disabled until a file is
 * chosen; a refusal (a draft no longer awaiting the signature, a second
 * upload, a type the bytes belie) travels the screen's own error path.
 */
function SignedUploadForm({
  busy,
  onSubmit,
  onCancel,
}: {
  busy: boolean
  onSubmit: (file: File) => Promise<void>
  onCancel: () => void
}) {
  const [file, setFile] = useState<File | null>(null)
  return (
    <form
      className="decision-form"
      onSubmit={(event) => {
        event.preventDefault()
        if (file !== null) void onSubmit(file)
      }}
    >
      <label>
        The client-signed PDF
        <input
          type="file"
          accept="application/pdf,.pdf"
          required
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
      </label>
      <p className="muted">
        The file is posted to the signature route with its own content type — no JSON wrapper — and
        the API checks the declared type against the bytes. Only a draft awaiting the signature
        admits the upload, and one upload only: a second is refused and stores nothing.
      </p>
      <span className="row-actions">
        <button type="submit" disabled={busy || file === null}>
          {busy ? 'Working…' : 'Upload the signed copy'}
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </span>
    </form>
  )
}

/**
 * The statement confirmation (`approve`, ruling R2): this draft makes NAR1
 * item 16's declaration and no approver has confirmed it yet, so the press is
 * gated on the tick — the submit posts `confirm_statement: true` and the API
 * records the approver and the moment on the draft. A draft without a
 * statement never meets this form; a confirmation bypassed some other way
 * answers the API's own 422 `statement_confirmation_required`.
 */
function StatementApproveForm({
  busy,
  onSubmit,
  onCancel,
}: {
  busy: boolean
  onSubmit: () => Promise<void>
  onCancel: () => void
}) {
  const [confirmed, setConfirmed] = useState(false)
  return (
    <form
      className="decision-form"
      onSubmit={(event) => {
        event.preventDefault()
        if (confirmed) void onSubmit()
      }}
    >
      <label>
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        I have checked the declaration at item 16 and confirm it stands for this draft.
      </label>
      <p className="muted">
        This draft makes the NAR1 private-company statement and it is not confirmed yet — the
        approval records the confirmation (<code>statement_confirmed_by/at</code>) with the approval
        stamp, and the export gate asks the same pair on the way out. The API refuses an approval
        without the tick (<code>statement_confirmation_required</code>); the draft does not move.
      </p>
      <span className="row-actions">
        <button type="submit" disabled={busy || !confirmed}>
          {busy ? 'Working…' : 'Approve — with the statement confirmed'}
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </span>
    </form>
  )
}

/**
 * The export's mode chooser (`export`): §2.12's three values, each with one
 * line about what it produces. The chosen mode rides the exports route's body
 * — the canonical door (ruling R4) — and the reply, the FilingPackage, is
 * held by the screen and shown with its document ids and envelope. No mode
 * submits anything: the e-Registry prep is a helper, the paper mode covers a
 * human lodgement, the api-ready JSON waits for a future channel.
 */
function ExportModeForm({
  busy,
  onSubmit,
  onCancel,
}: {
  busy: boolean
  onSubmit: (mode: ExportMode) => Promise<void>
  onCancel: () => void
}) {
  const [mode, setMode] = useState<ExportMode>('e_registry_prep')
  return (
    <form
      className="decision-form"
      onSubmit={(event) => {
        event.preventDefault()
        void onSubmit(mode)
      }}
    >
      <fieldset className="field-group">
        <legend>The package&rsquo;s mode</legend>
        {EXPORT_MODES.map((choice) => (
          <label key={choice}>
            <input
              type="radio"
              name="export_mode"
              value={choice}
              checked={mode === choice}
              onChange={() => setMode(choice)}
            />
            {exportModeLabel(choice)} — {exportModeDescription(choice)}
          </label>
        ))}
      </fieldset>
      <p className="muted">
        The export stores the artifacts as WORM documents, submits the draft&rsquo;s applied change
        events, and moves the draft to exported — one package per draft, and every refusal (the
        statement gate, a stale status) raises before anything is written.
      </p>
      <span className="row-actions">
        <button type="submit" disabled={busy}>
          {busy ? 'Working…' : 'Export in this mode'}
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </span>
    </form>
  )
}
