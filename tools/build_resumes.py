#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import zipfile
from xml.etree import ElementTree
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "resume.yaml"
DEFAULT_CI_OUTPUT_DIR = ROOT / "generated-resumes"
DEFAULT_LOCAL_OUTPUT_DIR = Path.home() / "Downloads" / "resumes"
DEFAULT_CV_TEMPLATE_DIR = ROOT / "templates" / "cv"
DEFAULT_RESUME_TEMPLATE_DIR = ROOT / "templates" / "resume"
DEFAULT_COVER_LETTER_TEMPLATE_DIR = ROOT / "templates" / "cover-letter"
DEFAULT_TEMPLATE_IDS = ["01", "02", "03", "04", "05"]
DEFAULT_SECTIONS = [
    "profile.md",
    "experience.md",
    "projects.md",
    "personal-projects.md",
    "leadership.md",
    "skills.md",
    "education.md",
    "certifications.md",
]
DEFAULT_RESUME_SECTIONS = [
    "profile.md",
    "experience-short.md",
    "skills.md",
    "education.md",
]
VARIANT_KEYS = {"cv", "resume", "cover-letter"}


def running_in_ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true" or bool(
        os.environ.get("CI")
    )


def default_output_dir() -> Path:
    return DEFAULT_CI_OUTPUT_DIR if running_in_ci() else DEFAULT_LOCAL_OUTPUT_DIR


def slugify(value: str) -> str:
    cleaned = re.sub(r"\s+", "-", value.strip())
    cleaned = re.sub(r"[^a-zA-Z0-9\-]", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned.strip("-").lower()


def resolve_repo_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (ROOT / path).resolve()


def resolve_slug(resume: dict[str, Any], variant_name: str | None = None) -> str:
    slug_source = str(resume.get("slug") or resume.get("id") or resume.get("name") or "").strip()
    if not slug_source:
        raise ValueError("Each resume entry must include one of: slug, id, or name")
    if variant_name is None:
        return slugify(slug_source)
    return slugify(str(resume.get("slug") or f"{slug_source}-{variant_name}"))


def load_config(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    resumes = data.get("resumes")
    if isinstance(resumes, list):
        shared_defaults = {k: v for k, v in data.items() if k != "resumes"}
        merged_resumes: list[dict[str, Any]] = []
        for item in resumes:
            if not isinstance(item, dict):
                continue
            merged = dict(shared_defaults)
            merged.update(item)
            merged_resumes.append(merged)
        return merged_resumes
    if isinstance(data, dict):
        return [data]
    raise ValueError(f"Invalid config format in {path}")


def iter_variants(resume: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    variants: list[tuple[str | None, dict[str, Any]]] = []
    parent_slug = resolve_slug(resume, None)
    for variant_name in ("cv", "resume", "cover-letter"):
        variant = resume.get(variant_name)
        if isinstance(variant, dict):
            merged = {k: v for k, v in resume.items() if k not in VARIANT_KEYS}
            merged.update(variant)
            merged["_variant_name"] = variant_name
            explicit_variant_slug = str(variant.get("slug") or "").strip()
            merged["_variant_slug"] = slugify(
                explicit_variant_slug or f"{parent_slug}-{variant_name}"
            )
            variants.append((variant_name, merged))
    if variants:
        return variants
    flat = dict(resume)
    flat["_variant_name"] = None
    flat["_variant_slug"] = resolve_slug(flat, None)
    return [(None, flat)]


def resolve_template_dir(resume: dict[str, Any], global_template_dir: Path | None = None) -> Path:
    if global_template_dir is not None:
        configured = global_template_dir.resolve()
        variant_name = resume.get("_variant_name")
        if variant_name in VARIANT_KEYS and configured.name not in VARIANT_KEYS:
            return configured / str(variant_name)
        return configured
    template_dir = resume.get("template_dir")
    if template_dir:
        configured = resolve_repo_path(str(template_dir))
        variant_name = resume.get("_variant_name")
        if variant_name in VARIANT_KEYS and configured.name not in VARIANT_KEYS:
            return configured / str(variant_name)
        return configured
    if resume.get("_variant_name") == "resume":
        return DEFAULT_RESUME_TEMPLATE_DIR
    if resume.get("_variant_name") == "cover-letter":
        return DEFAULT_COVER_LETTER_TEMPLATE_DIR
    return DEFAULT_CV_TEMPLATE_DIR


def resolve_markdown_content(resume: dict[str, Any]) -> str:
    section_files = resolve_section_files(resume)
    if not section_files:
        slug = str(resume.get("_variant_slug") or resolve_slug(resume)).strip()
        directory = str(resume.get("directory") or f"profiles/{slug}").strip()
        raise FileNotFoundError(f"No resume section files found in {resolve_repo_path(directory)}")
    return "\n\n".join(section.read_text(encoding="utf-8").rstrip() for section in section_files) + "\n"


def resolve_section_files(resume: dict[str, Any]) -> list[Path]:
    slug = str(resume.get("_variant_slug") or resolve_slug(resume)).strip()
    directory = str(resume.get("directory") or f"profiles/{slug}").strip()
    resume_dir = resolve_repo_path(directory)
    sections = resume.get("sections") or (
        DEFAULT_RESUME_SECTIONS
        if resume.get("_variant_name") == "resume"
        else ["cover-letter.md"]
        if resume.get("_variant_name") == "cover-letter"
        else DEFAULT_SECTIONS
    )
    if not isinstance(sections, list):
        raise ValueError(f"sections must be a list for resume '{slug}'")

    return [resume_dir / section for section in sections if (resume_dir / section).is_file()]


def resolve_split_markdown_content(resume: dict[str, Any]) -> tuple[str, str, str]:
    section_files = resolve_section_files(resume)
    if not section_files:
        slug = str(resume.get("_variant_slug") or resolve_slug(resume)).strip()
        directory = str(resume.get("directory") or f"profiles/{slug}").strip()
        raise FileNotFoundError(f"No resume section files found in {resolve_repo_path(directory)}")

    right_section_names = {"skills.md", "education.md"}
    full_parts: list[str] = []
    left_parts: list[str] = []
    right_parts: list[str] = []

    for section in section_files:
        text = section.read_text(encoding="utf-8").rstrip()
        full_parts.append(text)
        if section.name.lower() in right_section_names:
            right_parts.append(text)
        else:
            left_parts.append(text)

    full_markdown = "\n\n".join(full_parts) + "\n"
    left_markdown = "\n\n".join(left_parts).rstrip() + ("\n" if left_parts else "")
    right_markdown = "\n\n".join(right_parts).rstrip() + ("\n" if right_parts else "")
    return full_markdown, left_markdown, right_markdown


def render_markdown_fragment_to_latex(
    pandoc: str, markdown_content: str, metadata: dict[str, Any]
) -> str:
    if not markdown_content.strip():
        return ""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        tmp.write(yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True))
        metadata_file = Path(tmp.name)
    try:
        result = subprocess.run(
            [
                pandoc,
                "--from",
                "markdown",
                "--to",
                "latex",
                "--metadata-file",
                str(metadata_file),
            ],
            input=markdown_content.encode("utf-8"),
            stdout=subprocess.PIPE,
            check=True,
        )
        return result.stdout.decode("utf-8")
    finally:
        metadata_file.unlink(missing_ok=True)


def _ensure_certifi() -> Any | None:
    try:
        return importlib.import_module("certifi")
    except ImportError:
        return None


def _create_ssl_context() -> ssl.SSLContext:
    certifi_module = _ensure_certifi()
    if certifi_module is not None:
        return ssl.create_default_context(cafile=certifi_module.where())
    return ssl.create_default_context()


def download_logo(url: str, destination: Path) -> Path:
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix or ".png"
    target = destination.with_suffix(ext)
    context = _create_ssl_context()
    try:
        with urlopen(url, context=context) as response:  # nosec B310
            target.write_bytes(response.read())
    except URLError as exc:
        raise RuntimeError(f"Failed to download certification logo: {url}") from exc
    return target


def prepare_metadata_for_pdf(
    resume: dict[str, Any], slug: str, logos_dir: Path, include_logos: bool = True
) -> dict[str, Any]:
    metadata = dict(resume)
    for key in (
        "_variant_name",
        "_variant_slug",
        "directory",
        "sections",
        "slug",
        "id",
        "template_dir",
        "templates_to_build",
        "tempaltes_to_build",
        "cv",
        "resume",
    ):
        metadata.pop(key, None)

    logos = metadata.get("certification_logo_images")
    if not include_logos:
        metadata.pop("certification_logo_images", None)
    elif isinstance(logos, list) and logos:
        rewritten: list[Any] = []
        logos_dir.mkdir(parents=True, exist_ok=True)
        for idx, logo in enumerate(logos, start=1):
            if not isinstance(logo, dict):
                rewritten.append(logo)
                continue
            image_url = str(logo.get("image") or "").strip()
            if not image_url:
                raise ValueError(f"certification_logo_images[{idx}] is missing image URL for {slug}")
            local_path = download_logo(image_url, logos_dir / f"{slug}-{idx}")
            rewritten.append({**logo, "image": str(local_path.resolve())})
        metadata["certification_logo_images"] = rewritten
    return metadata


def resolve_latex_engine(requested: str) -> str:
    if shutil.which(requested):
        return requested
    if requested == "xelatex" and shutil.which("tectonic"):
        return "tectonic"
    raise FileNotFoundError(
        f"LaTeX engine not found: {requested}. Install xelatex/tectonic or run with --pdf-engine."
    )


def run_pandoc_pdf(
    pandoc: str,
    markdown_content: str,
    metadata: dict[str, Any],
    template_file: Path,
    output_pdf: Path,
    pdf_engine: str,
) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        tmp.write(yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True))
        metadata_file = Path(tmp.name)
    try:
        subprocess.run(
            [
                pandoc,
                "--from",
                "markdown",
                "--metadata-file",
                str(metadata_file),
                "--template",
                str(template_file),
                "--pdf-engine",
                pdf_engine,
                "-o",
                str(output_pdf),
            ],
            input=markdown_content.encode("utf-8"),
            check=True,
        )
    finally:
        metadata_file.unlink(missing_ok=True)


def run_pandoc_document(
    pandoc: str,
    markdown_content: str,
    metadata: dict[str, Any],
    output_file: Path,
    output_format: str,
) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        tmp.write(yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True))
        metadata_file = Path(tmp.name)
    try:
        subprocess.run(
            [
                pandoc,
                "--from",
                "markdown",
                "--to",
                output_format,
                "--metadata-file",
                str(metadata_file),
                "-o",
                str(output_file),
            ],
            input=markdown_content.encode("utf-8"),
            check=True,
        )
    finally:
        metadata_file.unlink(missing_ok=True)


def style_docx_header(output_file: Path) -> None:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    ElementTree.register_namespace("w", namespace)
    qname = lambda name: f"{{{namespace}}}{name}"
    with zipfile.ZipFile(output_file, "r") as source:
        entries = {info.filename: source.read(info.filename) for info in source.infolist()}

    document = ElementTree.fromstring(entries["word/document.xml"])
    header_paragraphs: list[ElementTree.Element] = []
    for paragraph in document.findall(f".//{qname('p')}"):
        text = "".join(node.text or "" for node in paragraph.findall(f".//{qname('t')}")).strip()
        if text:
            header_paragraphs.append(paragraph)
        if len(header_paragraphs) == 3:
            break

    sizes = (32, 22, 18)
    for paragraph, size in zip(header_paragraphs, sizes):
        properties = paragraph.find(qname("pPr"))
        if properties is None:
            properties = ElementTree.Element(qname("pPr"))
            paragraph.insert(0, properties)
        alignment = properties.find(qname("jc"))
        if alignment is None:
            alignment = ElementTree.SubElement(properties, qname("jc"))
        alignment.set(qname("val"), "center")
        spacing = properties.find(qname("spacing"))
        if spacing is None:
            spacing = ElementTree.SubElement(properties, qname("spacing"))
        spacing.set(qname("after"), "60")
        for run in paragraph.findall(f".//{qname('r')}"):
            run_properties = run.find(qname("rPr"))
            if run_properties is None:
                run_properties = ElementTree.Element(qname("rPr"))
                run.insert(0, run_properties)
            font_size = run_properties.find(qname("sz"))
            if font_size is None:
                font_size = ElementTree.SubElement(run_properties, qname("sz"))
            font_size.set(qname("val"), str(size))
            font_size_cs = run_properties.find(qname("szCs"))
            if font_size_cs is None:
                font_size_cs = ElementTree.SubElement(run_properties, qname("szCs"))
            font_size_cs.set(qname("val"), str(size))
            fonts = run_properties.find(qname("rFonts"))
            if fonts is None:
                fonts = ElementTree.SubElement(run_properties, qname("rFonts"))
            fonts.set(qname("ascii"), "Arial")
            fonts.set(qname("hAnsi"), "Arial")
        if paragraph is header_paragraphs[0]:
            run_properties = paragraph.find(f".//{qname('r')}/{qname('rPr')}")
            if run_properties is not None and run_properties.find(qname("b")) is None:
                ElementTree.SubElement(run_properties, qname("b"))

    entries["word/document.xml"] = ElementTree.tostring(
        document, encoding="utf-8", xml_declaration=True
    )
    temporary = output_file.with_suffix(".tmp.docx")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
        for filename, content in entries.items():
            target.writestr(filename, content)
    temporary.replace(output_file)


def build_docx_markdown(markdown_content: str, metadata: dict[str, Any]) -> str:
    name = str(metadata.get("name") or "").strip()
    title = str(metadata.get("title") or "").strip()
    contact_fields = [
        str(metadata.get(field) or "").strip()
        for field in ("location", "phone", "email", "linkedin", "website")
    ]
    contact_line = " | ".join(value for value in contact_fields if value)
    header_parts = [f"# {name}" if name else ""]
    if title:
        header_parts.append(title)
    if contact_line:
        header_parts.append(contact_line)
    header = "\n\n".join(part for part in header_parts if part)
    return f"{header}\n\n{markdown_content}" if header else markdown_content


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build configured resumes to Markdown, HTML, DOCX, and PDF formats."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: ~/Downloads/resumes locally, generated-resumes in CI).",
    )
    parser.add_argument(
        "--variant", choices=["all", "cv", "resume", "cover-letter"], default="all"
    )
    parser.add_argument("--slug", default=None, help="Build only matching variant slug")
    parser.add_argument("--template-dir", type=Path, default=None, help="Global template folder override")
    parser.add_argument("--pandoc", default="pandoc")
    parser.add_argument("--pdf-engine", default="xelatex")
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=["md", "html", "docx", "pdf"],
        default=["docx", "pdf"],
        help="Output formats to build (default: docx pdf).",
    )
    parser.add_argument("--clean", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not shutil.which(args.pandoc):
        raise FileNotFoundError(f"Pandoc executable not found: {args.pandoc}")
    resolved_engine = resolve_latex_engine(args.pdf_engine) if "pdf" in args.formats else None
    output_dir = (args.output_dir or default_output_dir()).resolve()
    local_default_output = not running_in_ci() and args.output_dir is None
    if (args.clean or local_default_output) and output_dir.exists():
        if output_dir.is_symlink():
            raise RuntimeError(f"Refusing to remove symlink output directory: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    resumes = load_config(args.config.resolve())
    built = 0
    matched_resume = False
    global_template_dir = args.template_dir.resolve() if args.template_dir else None

    for resume in resumes:
        for variant_name, variant in iter_variants(resume):
            if args.variant != "all" and variant_name != args.variant:
                continue
            slug = str(variant.get("_variant_slug") or resolve_slug(variant, variant_name))
            resume_id = slugify(str(variant.get("id") or slug))
            if args.slug and slug != args.slug:
                continue
            matched_resume = True

            markdown_content, left_markdown, right_markdown = resolve_split_markdown_content(variant)
            template_dir = resolve_template_dir(variant, global_template_dir)
            if "templates_to_build" in variant:
                selected_templates_raw = variant["templates_to_build"]
            elif "tempaltes_to_build" in variant:
                selected_templates_raw = variant["tempaltes_to_build"]
            else:
                selected_templates_raw = DEFAULT_TEMPLATE_IDS
            if not isinstance(selected_templates_raw, list):
                raise ValueError(f"Template selection must be a list for resume '{slug}'")
            selected_template_ids = [
                template_id
                for template_id in (
                    str(item).strip().rjust(2, "0") for item in selected_templates_raw
                )
                if (template_dir / f"{template_id}.tex").is_file()
            ]

            resume_output_dir = output_dir / resume_id
            docx_output_dir = resume_output_dir / "docx"
            pdf_output_dir = resume_output_dir / "pdf"
            resume_output_dir.mkdir(parents=True, exist_ok=True)
            if "docx" in args.formats:
                docx_output_dir.mkdir(parents=True, exist_ok=True)
            if "pdf" in args.formats:
                pdf_output_dir.mkdir(parents=True, exist_ok=True)
            metadata = prepare_metadata_for_pdf(
                resume=variant,
                slug=slug,
                logos_dir=resume_output_dir / "certification-logos",
                include_logos="pdf" in args.formats
                and variant_name != "cover-letter",
            )
            if "md" in args.formats:
                (resume_output_dir / f"{slug}.md").write_text(
                    markdown_content, encoding="utf-8"
                )
                built += 1
            if "html" in args.formats:
                run_pandoc_document(
                    pandoc=args.pandoc,
                    markdown_content=markdown_content,
                    metadata=metadata,
                    output_file=resume_output_dir / f"{slug}.html",
                    output_format="html",
                )
                built += 1
            if "docx" in args.formats or "pdf" in args.formats:
                if resolved_engine is None:
                    if "pdf" in args.formats:
                        raise RuntimeError("PDF output requires a configured LaTeX engine.")
                for template_id in selected_template_ids:
                    if "docx" in args.formats:
                        docx_output = docx_output_dir / f"{slug}-{template_id}.docx"
                        docx_metadata = dict(metadata)
                        docx_metadata.pop("title", None)
                        run_pandoc_document(
                            pandoc=args.pandoc,
                            markdown_content=build_docx_markdown(markdown_content, metadata),
                            metadata=docx_metadata,
                            output_file=docx_output,
                            output_format="docx",
                        )
                        style_docx_header(docx_output)
                        built += 1
                    if "pdf" in args.formats:
                        template_file = template_dir / f"{template_id}.tex"
                        if not template_file.is_file():
                            raise FileNotFoundError(f"Missing template: {template_file}")
                        output_pdf = pdf_output_dir / f"{slug}-{template_id}.pdf"
                        pdf_metadata = dict(metadata)
                        if template_id == "06":
                            pdf_metadata["left_body"] = render_markdown_fragment_to_latex(
                                args.pandoc, left_markdown, metadata
                            )
                            pdf_metadata["right_body"] = render_markdown_fragment_to_latex(
                                args.pandoc, right_markdown, metadata
                            )
                        run_pandoc_pdf(
                            pandoc=args.pandoc,
                            markdown_content=markdown_content,
                            metadata=pdf_metadata,
                            template_file=template_file,
                            output_pdf=output_pdf,
                            pdf_engine=resolved_engine,
                        )
                        built += 1

    if built == 0 and not matched_resume:
        raise ValueError("No matching resumes were built. Check --variant or --slug filters.")
    print(f"Built {built} output file(s) ({', '.join(args.formats)}) in: {output_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}")
        raise SystemExit(1)
