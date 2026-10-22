#!/usr/bin/env python3
"""Evidence-based ATS-style resume matching and tailoring.

This tool only uses content already present in profiles/*.md. It never adds a
missing job skill to the resume. Tailoring reorders verified bullets and adds
an evidence-backed alignment note to the generated copy.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import yaml

try:
    import certifi
except ImportError:
    certifi = None
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESUME_DIR = ROOT / "profiles"
RESUME_CONFIG = ROOT / "resume.yaml"
DEFAULT_OUTPUT = ROOT / "generated-resumes"


def resolve_resume_dir(value: str | None = None) -> Path:
    """Resolve the resume source directory from an explicit path or folder name."""
    configured_dirs: list[Path] = []
    if RESUME_CONFIG.exists():
        data = yaml.safe_load(RESUME_CONFIG.read_text(encoding="utf-8")) or {}
        resumes = data.get("resumes")
        if isinstance(resumes, list):
            for item in resumes:
                if not isinstance(item, dict):
                    continue
                directory = str(item.get("directory") or "").strip()
                if directory:
                    configured_dirs.append((ROOT / directory).resolve())

    if value is None:
        for configured in configured_dirs:
            if configured.exists():
                return configured
        if RESUME_DIR.exists():
            return RESUME_DIR.resolve()
        raise FileNotFoundError(
            f"No default resume directory found. Add 'directory' entries to {RESUME_CONFIG} "
            "or pass --resume-dir explicitly."
        )

    candidate = value
    path = Path(candidate)
    if not path.is_absolute():
        if path.exists():
            return path.resolve()
        for configured in configured_dirs:
            if configured.name == candidate:
                return configured
        if RESUME_CONFIG.exists():
            data = yaml.safe_load(RESUME_CONFIG.read_text(encoding="utf-8")) or {}
            resumes = data.get("resumes")
            if isinstance(resumes, list):
                for item in resumes:
                    if not isinstance(item, dict):
                        continue
                    resume_id = str(item.get("id") or "").strip()
                    resume_slug = str(item.get("slug") or "").strip()
                    directory = str(item.get("directory") or "").strip()
                    if directory and candidate in {resume_id, resume_slug}:
                        resolved = (ROOT / directory).resolve()
                        if resolved.exists():
                            return resolved
        candidate_path = RESUME_DIR / candidate
        if candidate_path.exists():
            return candidate_path.resolve()
        return (ROOT / candidate).resolve()
    return path.resolve()
STOP_WORDS = {
    "about", "above", "after", "again", "all", "also", "and", "any", "are",
    "been", "being", "between", "both", "build", "built", "can", "create",
    "development", "developed", "for", "from", "have", "into", "its", "more",
    "must", "our", "over", "should", "that", "their", "these", "this", "through",
    "using", "with", "will", "you", "your", "years", "work", "working",
}
JOB_SIGNAL_WORDS = re.compile(r"\b(required|preferred|skills?|technologies|tools?|qualifications|experience with|proficien)", re.IGNORECASE)
TECHNOLOGY_TERMS = {
    "airflow", "apache spark", "aws", "azure", "c++", "c#", "cuda", "cudf", "databricks",
    "dataproc", "docker", "eks", "emr", "gcp", "gpu", "gpus", "hadoop", "hive", "kafka",
    "kubernetes", "lakehouse", "linux", "mlflow", "mysql", "oracle", "pandas", "parquet", "orc",
    "json", "postgresql", "python", "rapids", "redis", "scala", "spark", "snowflake", "sql", "terraform", "yarn",
    "java", "javascript", "typescript", "react", "angular", "spring", "spring boot", "microservices",
    "rest", "grpc", "graphql", "git", "jenkins", "docker", "iceberg", "delta lake", "unity catalog",
    "arrow", "datafusion", "presto", "velox", "cccl", "ucx",
}
NON_SKILL_TERMS = {
    "nvidia", "company", "about", "role", "team", "teams", "job", "position", "location",
    "skills", "required", "preferred", "qualifications", "experience", "responsibilities",
    "description", "technical", "software", "systems", "system", "applications", "application",
    "engineering", "engineer", "developer", "development", "design", "designing", "work",
    "working", "strong", "excellent", "highly", "proficient", "ability", "understanding",
    "knowledge", "including", "related", "provide", "providing", "using", "written", "verbal", "and/or",
}
ALIASES = {
    "amazon web services": "aws",
    "aws": "aws",
    "amazon elastic kubernetes service": "eks",
    "k8s": "kubernetes",
    "kubernetes": "kubernetes",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "postgresql database": "postgresql",
    "continuous integration": "ci/cd",
    "continuous delivery": "ci/cd",
    "ci/cd": "ci/cd",
    "restful services": "rest",
    "restful service": "rest",
    "rest services": "rest",
    "micro-services": "microservices",
    "microservices": "microservices",
    "apache spark": "spark",
    "spring framework": "spring",
    "spring boot": "spring boot",
    "github copilot": "github copilot",
    "artificial intelligence": "ai",
    "machine learning": "machine learning",
}

@dataclass(frozen=True)
class Term:
    value: str
    normalized: str
    kind: str

@dataclass
class Match:
    term: Term
    status: str
    evidence: list[str]


class JobPageParser(HTMLParser):
    """Extract visible text while skipping page chrome and executable content."""

    ignored_tags = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form"}

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.ignored_tags:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.ignored_tags and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            value = html.unescape(re.sub(r"\s+", " ", data)).strip()
            if value:
                self.parts.append(value)

    def text(self) -> str:
        return "\n".join(self.parts)


def normalize(value: str) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[\u2010-\u2015\u2212-]", "-", value)
    value = re.sub(r"[^a-z0-9+#/.-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return ALIASES.get(value, value)


def fetch_job_url(url: str) -> str:
    workday_text = fetch_workday_job(url)
    if workday_text:
        return workday_text
    request = Request(url, headers={"User-Agent": "resume-match/1.0"})
    ssl_context = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()
    try:
        with urlopen(request, context=ssl_context, timeout=20) as response:
            content_type = response.headers.get_content_type()
            body = response.read()
    except (HTTPError, URLError, TimeoutError) as error:
        hint = " Install certifi with 'python3 -m pip install certifi'." if certifi is None else ""
        raise RuntimeError(f"Unable to fetch job URL: {error}.{hint}") from error
    if content_type != "text/html":
        raise RuntimeError(f"Unsupported job URL content type: {content_type}; use --job-file for exported text")
    parser = JobPageParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    text = parser.text()
    if len(text) < 100:
        raise RuntimeError("The job page did not contain enough visible text; it may require JavaScript or authentication")
    return text


def fetch_workday_job(url: str) -> str | None:
    """Fetch Workday's public JSON API for pages rendered by JavaScript."""
    parsed = urlsplit(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 4 or "job" not in path_parts:
        return None
    job_index = path_parts.index("job")
    if job_index == 0 or len(path_parts) <= job_index + 2:
        return None
    site = path_parts[job_index - 1]
    location = path_parts[job_index + 1]
    job_id = path_parts[job_index + 2]
    tenant = parsed.hostname.split(".")[0] if parsed.hostname else ""
    if not tenant or "workdayjobs" not in (parsed.hostname or ""):
        return None
    api_url = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/job/{location}/{job_id}"
    request = Request(api_url, headers={"User-Agent": "resume-match/1.0", "Accept": "application/json"})
    ssl_context = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()
    try:
        with urlopen(request, context=ssl_context, timeout=20) as response:
            if response.headers.get_content_type() != "application/json":
                return None
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None
    info = payload.get("jobPostingInfo", {})
    values = [info.get("title", ""), info.get("location", ""), info.get("jobDescription", "")]
    parser = JobPageParser()
    parser.feed("\n".join(str(value) for value in values if value))
    text = parser.text()
    return text if len(text) >= 100 else None


def read_resume(resume_dir: Path | None = None) -> tuple[str, list[str]]:
    target_dir = resume_dir or RESUME_DIR
    files = sorted(target_dir.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"No Markdown resume files found in {target_dir}")
    parts = [path.read_text(encoding="utf-8") for path in files]
    return "\n\n".join(parts), parts


def extract_resume_terms(text: str, resume_dir: Path | None = None) -> list[Term]:
    terms: list[Term] = []
    target_dir = resume_dir or RESUME_DIR
    skills_path = target_dir / "skills.md"
    if skills_path.exists():
        for category, values in re.findall(r"\*\*([^:]+):\*\*\s*(.+)", skills_path.read_text(encoding="utf-8")):
            for value in re.split(r"[,;]", values):
                value = value.strip().strip(".")
                if len(value) > 1:
                    terms.append(Term(value, normalize(value), "skill"))
    return unique_terms(terms)


def extract_job_terms(text: str, resume_terms: list[Term]) -> list[Term]:
    job_lower = text.lower()
    terms: list[Term] = []
    for resume_term in resume_terms:
        if len(resume_term.normalized) > 1 and re.search(r"(?<![a-z0-9])" + re.escape(resume_term.value.lower()) + r"(?![a-z0-9])", job_lower):
            terms.append(Term(resume_term.value, resume_term.normalized, resume_term.kind))
    for value in TECHNOLOGY_TERMS:
        if contains_term(value, text):
            terms.append(Term(value, normalize(value), "job-technology"))
    # Requirements and technology lines are parsed independently of the resume
    # catalog so a missing term can be reported instead of disappearing.
    for line in text.splitlines():
        if not JOB_SIGNAL_WORDS.search(line):
            continue
        for value in re.findall(r"\b[A-Za-z][A-Za-z0-9+#./-]{2,}\b", line):
            normalized = normalize(value)
            if is_technology_term(value, normalized):
                terms.append(Term(value, normalized, "job-requirement"))
    return unique_terms(terms)


def is_technology_term(value: str, normalized: str) -> bool:
    if normalized in NON_SKILL_TERMS or normalized in STOP_WORDS:
        return False
    if normalized in TECHNOLOGY_TERMS:
        return True
    if any(character in value for character in "+#/.") or any(character.isdigit() for character in value):
        return True
    if value.isupper() and len(value) >= 2:
        return True
    return value != value.lower() and any(character.isupper() for character in value[1:])


def unique_terms(terms: list[Term]) -> list[Term]:
    output: list[Term] = []
    seen: set[tuple[str, str]] = set()
    for term in terms:
        key = term.normalized
        if term.normalized and key not in seen:
            seen.add(key)
            output.append(term)
    return output


def evidence_for(term: Term, resume_text: str) -> list[str]:
    evidence = []
    for line in resume_text.splitlines():
        if line.strip() and contains_term(term.normalized, line):
            evidence.append(line.strip().lstrip("- "))
    return evidence[:3]


def contains_term(term: str, text: str) -> bool:
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    return re.search(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", normalize(text)) is not None


def compare(job_text: str, resume_text: str, resume_dir: Path | None = None) -> list[Match]:
    resume_terms = extract_resume_terms(resume_text, resume_dir)
    job_terms = extract_job_terms(job_text, resume_terms)
    experience_match = re.search(r"^# Experience\s*(.*?)(?=^# |\Z)", resume_text, flags=re.MULTILINE | re.DOTALL)
    experience_text = experience_match.group(1) if experience_match else ""
    matches: list[Match] = []
    for term in job_terms:
        evidence = evidence_for(term, resume_text)
        if evidence:
            has_experience = any(
                line.lstrip().startswith("-") and contains_term(term.normalized, line)
                for line in experience_text.splitlines()
            )
            status = "matched-exact" if has_experience else "matched-skills-only"
        else:
            status = "missing"
        matches.append(Match(term, status, evidence))
    return matches


def score(matches: list[Match]) -> int:
    if not matches:
        return 0
    weights = {"matched-exact": 1.0, "matched-related": 0.7, "matched-skills-only": 0.5, "missing": 0.0}
    return round(100 * sum(weights[item.status] for item in matches) / len(matches))


def render_report(job_source: str, matches: list[Match], generated_at: datetime) -> str:
    exact = [item for item in matches if item.status == "matched-exact"]
    related = [item for item in matches if item.status == "matched-related"]
    skills_only = [item for item in matches if item.status == "matched-skills-only"]
    missing = [item for item in matches if item.status == "missing"]
    supported = exact + related + skills_only
    lines = [
        "# ATS Match Report",
        "",
        f"**Job description:** `{job_source}`  ",
        f"**Generated:** {generated_at.isoformat(timespec='seconds').replace('+00:00', 'Z')}  ",
        f"**Evidence-based compatibility score:** {score(matches)}/100  ",
        "**Scoring note:** This is an explainable estimate, not the private score of a specific ATS.",
        "",
        "## Mismatch Summary",
        "",
        f"**Missing or unverified terms:** {len(missing)}",
        "",
    ]
    lines += [f"- **{item.term.value}**: not found in the verified resume source" for item in missing] or ["- None detected"]
    lines += [
        "",
        "## Matching Keywords",
        "",
    ]
    lines += [f"- **{item.term.value}** ({item.status})" for item in supported] or ["- None detected"]
    lines += ["", "## Evidence", ""]
    for item in supported:
        lines.append(f"### {item.term.value}")
        lines += [f"- {evidence}" for evidence in item.evidence]
    lines += ["", "## Guardrails", "", "- Missing keywords were not added to the resume.", "- Tailoring may only reorder or reuse verified resume content.", "- Review semantic matches before claiming equivalent experience."]
    return "\n".join(lines) + "\n"


def relevance(line: str, matches: list[Match]) -> int:
    normalized = normalize(line)
    return sum(1 for item in matches if item.status != "missing" and item.term.normalized in normalized)


def tailor_resume(resume_text: str, matches: list[Match]) -> str:
    sections = re.split(r"(?=^# )", resume_text, flags=re.MULTILINE)
    rendered: list[str] = []
    verified_terms = [item.term.value for item in matches if item.status != "missing"]
    for section in sections:
        if not section.strip():
            continue
        if section.startswith("# Experience"):
            blocks = re.split(r"(?=^## )", section, flags=re.MULTILINE)
            header, roles = blocks[0], blocks[1:]
            roles.sort(key=lambda block: max((relevance(line, matches) for line in block.splitlines()), default=0), reverse=True)
            section = header + "".join(roles)
        rendered.append(section.strip())
    alignment = ", ".join(verified_terms[:8])
    note = f"\n\n## Targeted Alignment\n\nThis version emphasizes verified experience relevant to: {alignment}." if alignment else ""
    return "\n\n".join(rendered) + note + "\n"


def read_clipboard_text() -> str | None:
    """Read text from the system clipboard when available."""
    import shutil
    import subprocess
    import sys

    commands = []
    if sys.platform == "darwin":
        commands = [["pbpaste"]]
    elif sys.platform.startswith("linux"):
        commands = [
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
            ["wl-paste"],
        ]
    elif sys.platform == "win32":
        commands = [["powershell", "-NoProfile", "-Command", "Get-Clipboard" ]]

    for command in commands:
        if shutil.which(command[0]) is None:
            continue
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            text = (result.stdout or "").strip()
            if text:
                return text
        except OSError:
            continue
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score and safely tailor a Markdown resume against a job description.")
    parser.add_argument("mode", choices=("score", "tailor", "both"), help="Report, tailored resume, or both")
    job_input = parser.add_mutually_exclusive_group()
    job_input.add_argument("--job-file", type=Path, help="Plain-text or Markdown job description")
    job_input.add_argument("--job-url", help="Public HTML job-description URL")
    parser.add_argument(
        "--resume-dir",
        type=str,
        default=None,
        help="Resume source path, profile folder name, or resume id/slug from resume.yaml",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT / "job-match", help="Output directory")
    parser.add_argument("--facts-file", type=Path, help="Optional user-provided facts; requires interactive confirmation")
    parser.add_argument("--pdf", action="store_true", help="Also render tailored-resume.pdf with Pandoc and XeLaTeX")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    resume_dir = resolve_resume_dir(args.resume_dir)
    if args.job_url:
        try:
            job_text = fetch_job_url(args.job_url)
        except RuntimeError as error:
            print(error, file=sys.stderr)
            return 2
        job_label = args.job_url
    elif args.job_file:
        if not args.job_file.exists():
            print(f"Job description not found: {args.job_file}", file=sys.stderr)
            return 2
        job_text = args.job_file.read_text(encoding="utf-8")
        job_label = str(args.job_file)
    else:
        clipboard_text = read_clipboard_text()
        if not clipboard_text:
            print("No job description provided. Pass --job-file, --job-url, or copy the URL/text to the clipboard.", file=sys.stderr)
            return 2
        if re.match(r"^https?://", clipboard_text, re.IGNORECASE):
            try:
                job_text = fetch_job_url(clipboard_text)
            except RuntimeError as error:
                print(error, file=sys.stderr)
                return 2
            job_label = clipboard_text
        else:
            job_text = clipboard_text
            job_label = "clipboard"
    resume_text, _ = read_resume(resume_dir)
    matches = compare(job_text, resume_text, resume_dir)
    args.output.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc)
    timestamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    report_path = args.output / f"match-report-{timestamp}.md"
    report_path.write_text(render_report(job_label, matches, generated_at), encoding="utf-8")
    print(f"ATS compatibility score: {score(matches)}/100")
    print(f"Report: {report_path}")
    if args.mode in ("tailor", "both"):
        tailored_path = args.output / "tailored-resume.md"
        tailored = tailor_resume(resume_text, matches)
        if args.facts_file:
            facts = args.facts_file.read_text(encoding="utf-8").strip()
            if facts:
                answer = input("Use these user-provided facts in the generated resume? [y/N] ")
                if answer.strip().lower() in {"y", "yes"}:
                    tailored += "\n## Confirmed Additional Facts\n\n" + facts + "\n"
                else:
                    print("User-provided facts were not added.")
        tailored_path.write_text(tailored, encoding="utf-8")
        print(f"Tailored resume: {tailored_path}")
        if args.pdf:
            pdf_path = args.output / "tailored-resume.pdf"
            subprocess.run(
                [
                    "pandoc",
                    str(tailored_path),
                    "--from", "markdown",
                    "--metadata-file", str(ROOT / "resume.yaml"),
                    "--template", str(ROOT.parent / "templates" / "cv" / "01.tex"),
                    "--pdf-engine=xelatex",
                    "-o", str(pdf_path),
                ],
                check=True,
            )
            print(f"Tailored PDF: {pdf_path}")
    if args.mode == "score":
        print("Missing skills remain recommendations only and were not added to the resume.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
