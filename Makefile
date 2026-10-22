OUTPUT_DIR := generated-resumes
RESUME_CONFIG ?= resume.yaml
PYTHON ?= python3
BUILD_SCRIPT := tools/build_resumes.py
DIRECT_LATEX_BUILD_SCRIPT := tools/build_resumes_direct_latex.py

.PHONY: build-markdown build-html build-docx build-pdf build-pdfs build-all build-direct-latex clean FORCE

FORCE:

build-markdown: FORCE
	$(PYTHON) $(BUILD_SCRIPT) --config $(RESUME_CONFIG) --output-dir $(OUTPUT_DIR) --formats md

build-html: FORCE
	$(PYTHON) $(BUILD_SCRIPT) --config $(RESUME_CONFIG) --output-dir $(OUTPUT_DIR) --formats html

build-docx: FORCE
	$(PYTHON) $(BUILD_SCRIPT) --config $(RESUME_CONFIG)

build-pdfs: FORCE
	$(PYTHON) $(BUILD_SCRIPT) --config $(RESUME_CONFIG)

build-pdf: build-pdfs

build-all: FORCE
	$(PYTHON) $(BUILD_SCRIPT) --config $(RESUME_CONFIG)

build-direct-latex: FORCE
	$(PYTHON) $(DIRECT_LATEX_BUILD_SCRIPT) --config $(RESUME_CONFIG) --output-dir $(OUTPUT_DIR) --variant all

clean:
	rm -rf $(OUTPUT_DIR)
