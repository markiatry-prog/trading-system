# Container for the trading-system foundation service.
#
# Serves /health and nothing else. That is the deliverable: proof the
# isolated stack deploys and can reach its own database. Pipeline stages
# arrive in their own tickets and will not need this file to change much.
#
# Runs unprivileged. No brokerage SDK is installed, and none may be added
# -- the execution-boundary guard blocks it in CI.

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY trading_system/ trading_system/
COPY scripts/ scripts/

RUN useradd --create-home --shell /usr/sbin/nologin trading
USER trading

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
EXPOSE 8080

CMD ["python", "scripts/run_service.py"]
