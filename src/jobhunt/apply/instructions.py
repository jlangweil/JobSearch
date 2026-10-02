"""Deep-link application checklist (SPEC 1.6). Nothing here submits anything."""

from __future__ import annotations

from collections.abc import Sequence

ATS_TIPS = {
    "workday": [
        "Workday requires an account per company; use the same email each time.",
        "Choose 'Apply Manually' (not 'Autofill with Resume') so fields are not mis-parsed, then upload the .docx.",
        "Re-check the Work Experience section Workday pre-fills; it often splits or merges roles.",
    ],
    "successfactors": [
        "SuccessFactors sites ask you to create a candidate profile first; the resume upload can pre-fill it.",
        "Review parsed employment dates — they are frequently shifted by a month.",
    ],
    "greenhouse": ["Greenhouse accepts .docx or .pdf; attach the cover letter in the separate field if present."],
    "lever": ["Lever parses the resume into the form; verify the current company and LinkedIn URL fields."],
    "ashby": ["Ashby forms are short; paste the cover letter text if there is no upload field."],
    "adzuna": ["This is an aggregator link: follow it through to the employer's own application page."],
}


def checklist(job: dict, rendered_paths: Sequence[str], answers: Sequence[dict]) -> dict:
    steps = [
        f"Open the application: {job.get('apply_url') or job.get('url')}",
        *(f"Upload {p}" for p in rendered_paths if p.lower().endswith((".docx", ".pdf"))),
    ]
    steps += ATS_TIPS.get(job.get("source") or "", [])
    for a in answers:
        if a.get("answer"):
            steps.append(f"Q: {a['question']} → {a['answer']}")
    steps.append("Submit the application yourself, then record it here with 'Confirm applied'.")
    return {"apply_url": job.get("apply_url") or job.get("url"), "steps": steps}
