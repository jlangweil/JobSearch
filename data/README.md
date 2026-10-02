# data/

Your personal files live here and are **git-ignored** (see `.gitignore`). Only the templates below are committed.

| File | In git? | What it is | How you get it |
|---|---|---|---|
| `facts.example.yaml` | yes | Template fact bank with placeholder roles, accomplishments, skills, a project, education and a certification | — |
| `facts.yaml` | **no** | **Your** fact bank: the only material tailored resumes and cover letters may use | `copy data\facts.example.yaml data\facts.yaml`, replace every entry, then `uv run jobhunt facts sync` |
| `apply_profile.example.yaml` | yes | Template of the answers auto-apply types into forms | — |
| `apply_profile.yaml` | **no** | **Your** name, address, phone, eligibility and EEO answers for application forms | Created on first use from `contact:` in `facts.yaml`; edit it on Settings → Auto-apply |
| `resume_template.docx` | yes | The Word *style* template documents are rendered with (fonts, margins, heading styles). No content. | — |
| `your-resume.docx` (any name) | **no** | **Your** original resume, to build `facts.yaml` from | Put it here; every `.docx` except the style template is ignored |
| `jobhunt.db`, `checkpoints.db` | **no** | The job database and workflow checkpoints | Created by `uv run jobhunt db upgrade` |

## Starting your own

1. Put your current resume in this folder, e.g. `data/my-resume.docx`. It stays on your machine.
2. Copy `facts.example.yaml` to `facts.yaml` and rewrite it from your resume:
   - one `role` per job;
   - one `achievement` per bullet, with its numbers in `metrics`;
   - your `skill` lines, `education` and `cert` facts;
   - a `project` for each portfolio project. Set `org` to its link (e.g. `github.com/you/repo`) and `tags` to its
     technologies. Its accomplishments are `achievement` facts with the project as `parent_id`.
3. Run `uv run jobhunt facts lint` and fix what it reports, then run `uv run jobhunt facts sync`.

Tailored resumes include a **Projects** section for the projects whose technologies a posting mentions. You can set
this under `tailoring.projects` in `config.yaml`.
