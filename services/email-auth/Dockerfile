FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
COPY app.py portal.py smoke.py candidate.py ozon_mcp.py ozon_details.py ozon_article_summary.py ozon_realization.py ozon_accrual_day.py ozon_return_events.py ozon_posting_status.py finance_gate.py market_cooldown.py wb_mcp.py external_keys.py external_mcp.py oauth_mcp.py gunicorn.conf.py ui.py ./
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--config", "gunicorn.conf.py", "--bind", "0.0.0.0:8000", "portal:create_portal()"]
