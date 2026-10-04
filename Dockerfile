# fabrication-prep API image (no slicer inside). Two stages:
#   build   — a virtualenv with the service and its dependencies (pip lives only here);
#   runtime — the same Python base with that virtualenv copied in and NO pip anywhere.
# Fleet rule: pip vendors its own dependencies and ships an SBOM of them, so every pip copy in a shipped
# image reports pip's vendored versions to the scanner. Nothing runs pip at runtime: uvicorn serves the app
# and the migrate init container runs `python -m fabrication_prep.cli migrate`. The final RUN fails the
# build if any pip copy survives. PYTHON_IMAGE lets a builder use a registry mirror of the same image.

ARG PYTHON_IMAGE=python:3.13-slim

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
RUN python -m venv /opt/venv
COPY pyproject.toml README.md LICENSE ./
COPY fabrication_prep ./fabrication_prep
RUN /opt/venv/bin/python -m pip install --no-cache-dir . \
 && /opt/venv/bin/python -m compileall -q /opt/venv/lib \
 && /opt/venv/bin/python -m pip uninstall --yes pip \
 && rm -f /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.13

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH FABRICATION_PREP_ENV=production
RUN /usr/local/bin/python3 -m pip uninstall --yes pip \
 && rm -rf /usr/local/lib/python3.13/ensurepip /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
 && groupadd -g 1001 app && useradd -u 1001 -g 1001 -M -d /nonexistent -s /usr/sbin/nologin app
COPY --from=build /opt/venv /opt/venv
RUN echo "--- surviving pip artifacts (must be empty) ---" \
 && ! find / -xdev \( -type d -name 'pip-*.dist-info' -o -type d -path '*/pip/_vendor' \
      -o -type f -name 'pip-*.whl' \) -print | grep . \
 && ! /usr/local/bin/python3 -c "import pip" 2>/dev/null \
 && ! /opt/venv/bin/python -c "import pip" 2>/dev/null \
 && /opt/venv/bin/python -c "import fabrication_prep.app" \
 && /opt/venv/bin/fabrication-prep check-profiles
USER 1001
WORKDIR /tmp
EXPOSE 8000
CMD ["uvicorn", "fabrication_prep.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]
