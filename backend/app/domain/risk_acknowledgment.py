"""The pre-use risk-acknowledgment statement and its version-compare rule.

The 2026-10-04 founder rulings require that before a deployment is used, the
operator is shown a risk-disclosure statement and explicitly confirms
understanding — every tier, no use until confirmed. This module owns the
statement source and the one rule the rest of the product speaks:

- **``STATEMENT_VERSION``** — the statement's version. The text is static and
  versioned by content, not by file (ruling R4): any wording change bumps
  the version, and every prior acceptance stops counting. The copy itself is
  founder-gated (the M3 precedent): v1.0 ships English only; a bilingual
  statement is a founder call.
- **``STATEMENT_TEXT``** — the full disclosure the API serves and the
  interstitial renders verbatim (ruling R5: the UI shows, never summarizes).
  It states the plan's three plain claims: the vendor control plane retains
  no client information (metadata-only metering); AI features forward
  minimized, redacted snippets to the LLM platform — SiliconFlow, the
  Singapore contracting entity under the PDPA — for further processing,
  under the provider's standard terms; and use of the AI features requires
  this acknowledgment.

**Copy status: v3 — drafted 2026-10-10 on the founder's 2026-10-08 ruling
(ADR-0019 §2, option 甲), founder approval rides the merge sitting** (the M3
precedent: the wording is customer-facing disclosure copy, and ruling R4's
version bump re-opens acceptance by construction — cost-free pre-launch). It
is **bilingual (EN/ZH)**, the 2026-10-07 settlement carried forward. v3
replaces v2's "Your company-level choices" section: the per-company opt-in
promise retires with the Q1.8 gate, and the new "Who decides" section states
the TCSP-layer basis — the operator's confirmation on the firm's behalf, no
per-company opt-in, and the objection valve (a company under objection
disables the features, nothing for it is sent). Every other disclosure
element the 2026-10-04 rulings require survives — the residual risk (the
external platform, its jurisdiction and its standard terms), the
metadata-only control plane, and the re-confirmation rule — and the copy
keeps what the product does with client details (plain placeholders) and
what it does when it cannot certify them (nothing is sent; the message goes
to staff).

- **``acceptance_counts``** — the version-compare rule: an acceptance counts
  only if it carries the version the code currently serves. The application
  read helper applies it to the latest acceptance event in the audit chain,
  so a statement edit re-opens the acknowledgment by construction — re-consent
  is automatic, never a migration of old rows.

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

# The served statement is bilingual copy: full-width CJK punctuation is correct
# in it, so the ambiguous-character rule is waived for this module (the same
# file-level waiver the other bilingual-copy modules carry).
# ruff: noqa: RUF001

STATEMENT_VERSION = "v3"

STATEMENT_TEXT = """\
AI features — risk disclosure and confirmation

This deployment includes AI features that help with company-secretarial work.
Before they can be used, an Administrator of this deployment must read this
statement and confirm that they understand it. Use of the AI features requires
that confirmation, and if this statement is ever revised, the revised statement
must be confirmed again.

What is sent. When the AI is used, only the content of the message is sent, with
names, HKID and passport numbers, addresses, phone numbers, emails and bank
account numbers replaced first by plain placeholders such as [Company 1] or
[Person 1]. The real details stay in this deployment, and whoever processes the
placeholders does not receive them. If the system cannot confirm that a detail
has been replaced, the message is not sent — it goes to your staff instead.

Where it goes. Those placeholders are forwarded to an external AI platform for
processing: SiliconFlow, the Singapore contracting entity, whose handling of
personal data is governed by Singapore's PDPA. That processing happens under the
provider's standard terms, which we do not control.

What is not sent. Operating the service — metering and protocol coordination —
uses metadata only. Client names, client data and document content are never
sent to the vendor control plane.

Who decides. These AI features are part of the service your firm provides, and
this confirmation records that you, as the deployment's Administrator, accept
the residual risk described above on your firm's behalf. There is no per-company
opt-in to record before use. If a client objects to the AI features for one of
its companies, your firm can disable them for that company: nothing for that
company is sent, and that company's mail is handled by your staff directly.

---
AI 功能 — 風險披露與確認

本部署包含協助公司秘書工作的 AI 功能。使用這些功能前，本部署的管理員必須閱讀本聲明並確認理解。
使用 AI 功能以此確認為前提；本聲明一經修訂，須就修訂版本重新確認。

發送什麼。使用 AI 時只發送訊息內容本身：姓名、身份證及護照號碼、地址、電話、電郵及銀行戶口號碼，
均會先被替換為 [公司1]、[人1] 這類簡單佔位符。真實資料留在本部署內，處理佔位符的一方不會收到
這些資料。若系統無法確認某項資料已被替換，該訊息不會被發送，而是轉交你的員工處理。

發送到哪裡。這些佔位符會被送往外部 AI 平台處理：SiliconFlow（新加坡實體），其個人資料處理受
新加坡《個人資料保護法》（PDPA）規管。該處理依該供應商的標準條款進行，我們無法控制。

不發送什麼。營運本服務（計量與協議協調）只使用元數據。客戶名稱、客戶資料及文件內容，永不
發送至供應商的控制面。

誰來決定。這些 AI 功能是你所提供服務的一部分；本確認記錄你作為本部署的管理員，代表你的公司
接受上述殘餘風險。使用前無需逐間公司選擇加入（opt-in）。如客戶反對其某間公司使用 AI 功能，
你的公司可為該公司停用：該公司的資料不會被發送，相關郵件由你的員工直接處理。
"""


def acceptance_counts(accepted_version: str | None) -> bool:
    """The version-compare rule (ruling R4): an acceptance counts only while
    it carries ``STATEMENT_VERSION`` — the version the code currently serves.
    ``None`` (no acceptance) and every older or newer version fail closed to
    not-acknowledged, so a wording change re-opens the acknowledgment for
    every deployment at once."""
    return accepted_version == STATEMENT_VERSION
