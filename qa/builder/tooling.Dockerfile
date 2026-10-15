# Minimal tooling substrate, NOT a stock Hermes/model acceptance image.
FROM python:3.12-slim
RUN python3 -m venv /opt/hermes/.venv \
 && /opt/hermes/.venv/bin/python -m pip install --no-cache-dir PyYAML==6.0.3
USER 1000:1000
ENV HOME=/work WIZARD_BUILDER_ROOT=/opt/wizard-builder PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
