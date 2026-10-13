/**
 * The signature-ready package a release just produced (scope §2.10): the
 * ``generated_draft`` document ``send_for_signature`` stored, and the fill's
 * escalations.
 *
 * It is a pure component rendered from the transition's own reply — the block
 * only ever arrives on the release's response — so what it shows is the API's
 * own words and nothing inferred here. The escalations are the human's
 * checklist: boxes the fill could not complete from the draft (the presentor's
 * reference, a licence's details, the §1 protected class), which never refuse
 * the release and are exactly what a person checking the package must
 * complete by hand.
 */

import type { SignaturePackage } from '../api/types'
import { signaturePackageEscalationLabel } from '../drafts'
import { shortId } from '../format'

export function SignaturePackagePanel({
  package: pkg,
}: {
  package: SignaturePackage
}) {
  return (
    <section className="panel">
      <h2>The signature-ready package</h2>
      <p className="muted">
        The release just filled the pinned CR form from this version&rsquo;s resolved values and
        stored the result as a WORM document. This block is the release&rsquo;s own reply — a
        re-read of the draft does not carry it, and the document is served by the records
        registry&rsquo;s download route
        (<code>GET /companies/&#123;company_id&#125;/documents/&#123;document_id&#125;/download</code>).
      </p>
      <table className="table">
        <tbody>
          <tr>
            <th scope="row">Document</th>
            <td>
              <code>{pkg.document_id}</code> <span className="muted">({shortId(pkg.document_id)})</span>
            </td>
          </tr>
        </tbody>
      </table>

      <h3>Complete by hand before filing</h3>
      {pkg.escalations.length === 0 ? (
        <p className="muted">
          The fill completed every box it fills from the draft — nothing is left for a human here.
          The form&rsquo;s own signature line is still signed by hand.
        </p>
      ) : (
        <>
          <p className="muted">
            The fill stopped short of these boxes rather than guessing: each is named with the
            path a person works from, and none of them refused the release — a draft awaiting a
            detail is ordinary work in progress.
          </p>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Kind</th>
                <th scope="col">What is left to complete</th>
              </tr>
            </thead>
            <tbody>
              {pkg.escalations.map((escalation, index) => (
                <tr key={`${escalation.kind}-${index}`}>
                  <td>
                    <span className="company-name">
                      {signaturePackageEscalationLabel(escalation.kind)}
                    </span>
                    <span className="brn">{escalation.kind}</span>
                  </td>
                  <td>{escalation.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}
