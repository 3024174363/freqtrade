FROM freqtradeorg/freqtrade:2026.10@sha256:0cc4b6dc9b16e0516d27efae1765e925da7d6aea5c8f3c3825930ffe3098d7c7

USER root
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
USER ftuser
COPY --chown=ftuser:ftuser freqtrade /workspace/freqtrade
WORKDIR /workspace/freqtrade
RUN pip install --user --no-cache-dir --no-deps -e . \
    && pip install --user --no-cache-dir pytest pytest-asyncio pytest-cov pytest-mock pytest-random-order pytest-timeout pytest-xdist time-machine ruff
ENV PYTHONPATH=/workspace/freqtrade
ENTRYPOINT ["freqtrade"]
