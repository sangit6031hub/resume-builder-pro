# Resume Builder

This repository is dedicated to generating resume content and PDF exports for one or more resume variants.

It is intentionally separated from the website repository so the website source and resume generation pipeline can evolve independently.

## Purpose

- Maintain resume content in Markdown by section
- Support multiple resume variants from the same repo
- Generate Markdown, HTML, DOCX, and PDF exports
- Build only selected templates per resume based on configuration
- Publish generated artifacts from GitHub Actions as release assets

## Repository layout

```text
resume-builder/
├── .github/
│   └── workflows/
│       └── build-resume.yml
├── Makefile
├── README.md
├── .gitignore
├── resume.yaml                # master resume configuration
├── generated-resumes/         # generated output files
├── tools/
│   ├── build_resumes.py
│   ├── build_resumes_direct_latex.py
│   └── resume_match.py
├── templates/
│   ├── cv/
│   │   ├── 01.tex
│   │   ├── 02.tex
│   │   ├── 03.tex
│   │   ├── 04.tex
│   │   └── 05.tex
│   └── profiles/
│       ├── 01.tex
│       ├── 02.tex
│       ├── 03.tex
│       ├── 04.tex
│       ├── 05.tex
│       └── 06.tex
├── profiles/
│   ├── san-resume/            # San's resume sections
│   │   ├── profile.md
│   │   ├── experience.md
│   │   ├── experience-short.md
│   │   ├── projects.md
│   │   ├── personal-projects.md
│   │   ├── leadership.md
│   │   ├── skills.md
│   │   ├── education.md
│   │   └── certifications.md
│   ├── sowji-software/        # QA / automation resume sections
│   │   ├── experience.md
│   │   ├── experience-short.md
│   │   ├── profile.md
│   │   ├── skills.md
│   │   ├── education.md
│   │   └── certifications.md
│   ├── sowji-da/              # dental assistant resume sections
│   │   ├── experience.md
│   │   ├── experience-short.md
│   │   ├── profile.md
│   │   ├── skills.md
│   │   └── education.md
```

## Resume configuration

The central configuration file is:

- `resume.yaml`

This file defines one or more resume entries and the details used for rendering, including:

- `id`
- `slug`
- `directory`
- `sections`
- `templates_to_build` (preferred property)
- `tempaltes_to_build` (backward-compatible typo alias)
- nested `cv` and `resume` variants for each person/resume
- metadata such as `name`, `title`, `location`, `phone`, `email`, `linkedin`, and `website`

Example:

```yaml
resumes:
  - id: sample-dev
    slug: alex-rivera
    name: Alex Rivera
    title: Software Engineer | Cloud & Automation
    location: Example City, California
    phone: "+1 555 010 0142"
    email: alex.rivera@example.com
    linkedin: https://www.linkedin.com/in/alex-rivera-example/
    website: https://example.com/alex-rivera
    cv:
      directory: profiles/san-resume
      sections:
        - profile.md
        - experience.md
        - projects.md
        - personal-projects.md
        - leadership.md
        - skills.md
        - education.md
        - certifications.md
      templates_to_build: [01, 04, 05]
      template_dir: templates/cv
    resume:
      directory: profiles/san-resume
      sections:
        - profile.md
        - experience-short.md
        - skills.md
        - education.md
      templates_to_build: [05]
      template_dir: templates/resume
```

Notes:

- If `templates_to_build` is omitted, all templates are used by default.
- The typo `tempaltes_to_build` is also supported for backward compatibility.
- Each resume entry can define both `cv` and `resume` variants with different section lists, template directories, and output slugs.
- A `cover-letter` variant can use `templates/cover-letter/` and build a paired DOCX/PDF cover letter.
- Missing section files are ignored gracefully; the build only includes files that exist.

## Templates

The CV template files in `templates/cv/` are numbered for direct selection:

- `01.tex`
- `02.tex`
- `03.tex`
- `04.tex`
- `05.tex`

These correspond to ATS, executive, technical, classic, and compact presentation styles.

The build reads the template IDs from config and only renders those template files. For example:

```yaml
templates_to_build: [04, 01]
```

The short resume templates in `templates/resume/` can be used with the shorter section set:

- `05.tex`
- `06.tex` (single-page focused style)

Cover letters use the ATS-safe template in `templates/cover-letter/01.tex` and
the profile's `cover-letter.md` section.

## Two-page resume setup

Use the `resume` variant for a concise version intended to stay within two pages:

- Keep only core sections: profile, experience-short, skills, education
- Limit bullets per role and avoid long tool inventories
- Prefer compact template(s), for example `04` or `01`

## Building resumes

From the repo root:

```bash
make build-markdown
make build-pdfs
make build-html
make build-docx
make build-all
```

### Common targets

- `make build-markdown`  Generates Markdown resume files for each configured resume.
- `make build-html`      Generates HTML resume outputs.
- `make build-docx`      Generates plain, single-column DOCX outputs suitable for ATS submission.
- `make build-pdfs`      Generates PDF outputs for the configured templates.
- `make build-all`       Builds markdown, HTML, DOCX, and PDFs.
- `make build-direct-latex` Builds resume-variant PDFs directly via LaTeX without intermediate markdown files.
- `make clean`           Removes generated output.

### Direct LaTeX mode (no intermediate markdown files)

You can build resume variants with a dedicated script:

```bash
python3 tools/build_resumes.py \
  --config resume.yaml \
  --output-dir generated \
  --variant resume
```

To build only a configured cover letter:

```bash
python3 tools/build_resumes.py \
  --config resume.yaml \
  --output-dir generated \
  --variant cover-letter
```

Notes:
- This mode reads section markdown files directly and renders LaTeX in memory.
- It outputs the same numbered filenames as the standard builder (for example `slug-06.pdf`).

## Output directory

Generated artifacts are written to:

- `generated-resumes/`

Every normal builder run generates both DOCX and PDF outputs. For ATS
submissions, the DOCX files are produced directly from the profile Markdown
using a normal single-column document flow; they do not include the PDF
template's logos, colors, columns, or graphics.

When run locally without `--output-dir`, generated DOCX and PDF files are
written under `~/Downloads/resumes/<profile>/docx/` and
`~/Downloads/resumes/<profile>/pdf/`. Certification logo assets are stored
under `~/Downloads/resumes/<profile>/certification-logos/`. The existing
`~/Downloads/resumes/` directory is replaced before a local build so stale
files are not mixed with the new outputs. GitHub Actions continues to write
to `generated-resumes/<profile>/docx/`,
`generated-resumes/<profile>/pdf/`, and
`generated-resumes/<profile>/certification-logos/`.

DOCX files use the same template IDs as their paired PDF files, for example
`alex-rivera-cv-01.docx` and `alex-rivera-cv-01.pdf`.
The DOCX files intentionally share the ATS-safe layout rather than copying
decorative or multi-column PDF styling.

DOCX headers are centered and styled with a larger bold name, a readable title,
and a compact contact line. The header remains ordinary selectable text in the
document body; it is not a Word header, table, image, or text box.

Examples:

```text
generated-resumes/
├── alex-rivera.md
├── alex-rivera-01.pdf
├── alex-rivera-04.pdf
└── ...
```

## Multi-resume support

The repo supports multiple resume variants in a single build setup. Each variant can have its own:

- folder path
- section list
- metadata
- selected templates

The system loads each entry from `resume.yaml` and creates the generated output for each resume independently.

## CI / GitHub Actions

The workflow in:

- `.github/workflows/build-resume.yml`

runs on pushes, pull requests, and manual dispatch and performs the following:

1. checks out the repository
2. fetches tags
3. installs Pandoc and PDF tooling
4. runs `make build-markdown build-pdfs`
5. creates a version tag when appropriate
6. publishes the generated resume markdown and PDFs as GitHub release assets
7. uploads the output as workflow artifacts

## Notes

- This repo is separate from the website repo and is meant only for resume generation.
- The resume build should not fail when some resume section files are absent. The build includes only files that exist.
- The workflow is designed to produce release-quality resume exports without mixing resume generation with the Jekyll website source.

## Related repositories

- Website repo: `my-resume`
- Resume repo: `resume-builder`

This separation keeps the annual website content and the resume-generation pipeline clean and independently maintainable.
