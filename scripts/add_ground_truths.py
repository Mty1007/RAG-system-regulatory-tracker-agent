"""Add ground_truth answers to the 10 best rows in rag_eval_log.csv.

Rows selected: those with complete, substantive answers (not "context does
not contain..." responses). Ground truths are written from the source
regulatory text visible in the context columns of those rows.

Run once:
    python scripts/add_ground_truths.py
"""

import csv
import pathlib
import shutil
import uuid

LOG_PATH = pathlib.Path("rag_eval_log.csv")

# Ground truths keyed on input_text (question text).
# Only questions with clear, complete answers are included.
GROUND_TRUTHS: dict[str, str] = {
    "What are the SFC requirements for client asset segregation?": (
        "The SFC requires client assets to be held in segregated accounts with "
        "authorised financial institutions. Client money must be deposited into "
        "a segregated account within one business day of receipt. Futures brokers "
        "must book client positions and related margins separately from their "
        "proprietary positions. For virtual assets, at least 98% must be held in "
        "cold storage such as HSM-based cold storage, segregated from the platform "
        "operator's own assets."
    ),
    "How should a licensed corporation handle client money received?": (
        "A licensed corporation must pay client money into a segregated account "
        "maintained with an authorised financial institution in Hong Kong within "
        "one business day of receipt. If the money is received outside Hong Kong, "
        "it must be paid into a segregated account with a bank in another "
        "jurisdiction as agreed by the SFC. The money must not be commingled with "
        "the firm's own funds."
    ),
    "What are the obligations of a futures broker regarding client margin?": (
        "A futures broker must not trade futures contracts for a client unless the "
        "client has provided sufficient collateral to meet the margin requirement. "
        "The broker must not grant credit facilities or loans to help clients meet "
        "margin requirements, except for concessionary margining. Detailed records "
        "of margin calls including amounts, times, client responses, and follow-up "
        "actions must be maintained. Clients with margin shortfalls must not be "
        "permitted to open new positions that increase the shortfall."
    ),
    "What are the licensing requirements for virtual asset service providers under SFC?": (
        "Centralised virtual asset trading platforms intending to offer trading of "
        "at least one security token must apply to the SFC for a Type 1 (dealing "
        "in securities) and Type 7 (providing automated trading services) licence. "
        "Platform operators must comply with all licensing conditions; any breach "
        "constitutes misconduct under the SFO. They must also meet AML/CFT "
        "requirements under the AMLO and may be required to undergo an external "
        "assessment as part of the licensing process."
    ),
    "What are the SFC guidelines on managing conflicts of interest?": (
        "Key operators must avoid situations where conflicts of interest may arise. "
        "Where conflicts cannot be avoided, investors' interests must be "
        "sufficiently protected. Organisations must maintain effective arrangements "
        "to identify, prevent, manage, and monitor conflicts of interest. "
        "Transactions must be conducted in good faith, at arm's length, and in the "
        "best interests of clients. Personal account dealings must be conducted "
        "properly to avoid conflicts when advising clients."
    ),
    "What does PCPD require for data retention and disposal?": (
        "Under DPP 2 of the PDPO, data users must take all practicable steps to "
        "ensure personal data is not kept longer than is necessary for the purpose "
        "for which it was collected. Section 26 of the PDPO requires data users to "
        "erase personal data when it is no longer required. Where a data processor "
        "is engaged, the data user must use contractual or other means to prevent "
        "the data from being retained beyond the necessary period. Data on portable "
        "storage devices should be securely erased after each use."
    ),
    "What are the notification requirements when a data breach occurs under PCPD?": (
        "There is no statutory requirement under the PDPO to notify the PCPD of a "
        "data breach, but it is recommended best practice to do so. Data users "
        "should notify both the PCPD and affected data subjects as soon as "
        "practicable after becoming aware of a breach, especially where there is a "
        "real risk of harm. Notification should include all available details about "
        "the breach and can be submitted using the PCPD's Data Breach Notification "
        "Form online, by fax, in person, or by post."
    ),
    "What obligations does a data user have when transferring data outside Hong Kong?": (
        "A data user must not transfer personal data outside Hong Kong unless one "
        "of the following conditions is met: the destination is specified in a "
        "notice under section 33; the user has reasonable grounds to believe the "
        "destination has laws substantially similar to the PDPO; or the data "
        "subject has given written consent. Where a data processor outside Hong "
        "Kong is engaged, the data user must adopt contractual or other means to "
        "prevent unauthorised access, processing, erasure, loss, or use of the "
        "transferred data."
    ),
    "What are the requirements for cold storage of virtual assets?": (
        "Platform operators and their associated entities must store at least 98% "
        "of client virtual assets in cold storage, such as HSM-based cold storage, "
        "except under limited circumstances approved by the SFC on a case-by-case "
        "basis. Transactions out of cold storage must be minimised. Virtual assets "
        "in cold storage must be segregated from the platform operator's own assets "
        "and covered under the required compensation arrangement. Storage solutions "
        "must be kept up to date in light of evolving security threats."
    ),
    "Are there any exemptions from SFC licensing requirements?": (
        "The SFC may grant exemptions from the Regulatory Industry Qualification "
        "or Licensing Representative Paper requirements if an individual can "
        "demonstrate comparable qualifications or industry experience. Individuals "
        "who have been licensed by the SFC within the past three years applying for "
        "the same regulated activities in the same role may apply for a full "
        "exemption. Conditional exemptions are also available for those with at "
        "least five years of related local experience over the past eight years. "
        "The SFC may impose additional licensing conditions when granting any "
        "exemption."
    ),
}


def main() -> None:
    if not LOG_PATH.exists():
        print(f"ERROR: {LOG_PATH} not found — run the API first.")
        return

    # Read all rows
    with LOG_PATH.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    # Track which questions we've already assigned a ground truth
    # (take only the first occurrence so we don't duplicate)
    assigned: set[str] = set()
    updated = 0

    for row in rows:
        q = row.get("input_text", "").strip()
        if q in GROUND_TRUTHS and q not in assigned and not row.get("ground_truth", "").strip():
            row["ground_truth"] = GROUND_TRUTHS[q]
            assigned.add(q)
            updated += 1

    # Back up the original, then write the updated file
    backup = LOG_PATH.with_suffix(".csv.bak")
    shutil.copy(LOG_PATH, backup)

    with LOG_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Updated {updated} rows with ground_truth answers.")
    print(f"Original backed up to {backup}")
    print(f"\nRe-run `python scripts/eval_quality.py` to score with answer_similarity.")


if __name__ == "__main__":
    main()
