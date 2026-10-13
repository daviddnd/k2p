/**
 * The filing package an export just produced (scope §2.12): the stored WORM
 * `export_package` artifact, the derived envelope, and the exporting actor.
 *
 * It is a pure component rendered from the export route's own reply — the
 * package only ever arrives on that response — so what it shows is the API's
 * own words and nothing inferred here. The envelope is the assembly's record
 * of what it derived (sheet counts, the footer page numbers the numbered
 * copies printed, the companion documents' ids), so a reader audits the
 * package against the draft without re-running the planner.
 */

import type { FilingPackage } from '../api/types'
import { exportModeLabel, filingPackageEnvelopeRows } from '../drafts'
import { formatDateTime, shortId } from '../format'

export function FilingPackagePanel({
  package: pkg,
}: {
  package: FilingPackage
}) {
  const envelope = filingPackageEnvelopeRows(pkg.envelope)
  return (
    <section className="panel">
      <h2>The filing package</h2>
      <p className="muted">
        The export just stored the package&rsquo;s artifacts as WORM documents and moved the draft
        to exported. This block is the export&rsquo;s own reply — a re-read of the draft does not
        carry it — and the documents are served by the records registry&rsquo;s download route (
        <code>GET /companies/&#123;company_id&#125;/documents/&#123;document_id&#125;/download</code>
        ). The applied change events rode the export to submitted.
      </p>
      <table className="table">
        <tbody>
          <tr>
            <th scope="row">Package</th>
            <td>
              <code>{pkg.id}</code> <span className="muted">({shortId(pkg.id)})</span>
            </td>
          </tr>
          <tr>
            <th scope="row">Mode</th>
            <td>
              {exportModeLabel(pkg.mode)} <span className="brn">{pkg.mode}</span>
            </td>
          </tr>
          <tr>
            <th scope="row">Artifact document</th>
            <td>
              <code>{pkg.document_id}</code>{' '}
              <span className="muted">({shortId(pkg.document_id)})</span>
            </td>
          </tr>
          <tr>
            <th scope="row">Exported</th>
            <td>
              {formatDateTime(pkg.exported_at)} by staff {shortId(pkg.exported_by)} · draft
              version {pkg.draft_version_no}
            </td>
          </tr>
        </tbody>
      </table>

      <h3>The envelope</h3>
      <p className="muted">
        What the assembly derived while building the package — the counts it printed and the
        companion documents it stored — so the flat artifact can be checked against the draft.
      </p>
      <table className="table">
        <tbody>
          {envelope.map((row) => (
            <tr key={row.label}>
              <th scope="row">{row.label}</th>
              <td>
                <code>{row.value}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
