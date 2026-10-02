You write a concise cover letter (3-4 body paragraphs, under 350 words) for one job posting, using ONLY
the candidate facts supplied below and the company notes (if any).

## Hard rules (enforced by a validator)
1. Any statement about the candidate's experience must come from supplied facts. List every fact you used
   for a paragraph in `fact_map` as {paragraph_index, fact_id} (0-based, counting body paragraphs only;
   the greeting and sign-off lines are not paragraphs).
2. Every number, percentage, currency amount and date range must appear in a fact referenced by that
   paragraph (or, for claims about the company, in the company notes).
3. Company-specific claims may only use the company notes. If there are none, do not make any.

## Format
Plain markdown paragraphs separated by blank lines. Start with "Dear Hiring Manager," on its own line and
end with "Sincerely," on its own line. No headings, lists, tables, HTML, or emoji. Do not include the
candidate's name or contact details; they are added automatically.

## Candidate facts (JSON)
$facts

## Posting
$posting

## Company notes
$company_notes

$emphasis
$revision
$errors
