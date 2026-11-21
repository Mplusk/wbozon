FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
COPY app.py portal.py smoke.py candidate.py ozon_mcp.py ozon_details.py ozon_article_summary.py ozon_realization.py ozon_accrual_day.py ozon_return_events.py ozon_posting_status.py finance_gate.py market_cooldown.py wb_mcp.py external_keys.py external_mcp.py oauth_mcp.py gunicorn.conf.py ui.py ./
COPY ozon_unallocated.py ozon_fbo.py ozon_supply_allocation.py ./
COPY avito_mcp.py avito_views.py avito_contracts.json ./
COPY market_catalog.py market_actions.py market_https.py market_routes.jsonl ./
COPY market_specs.py market_transport.py expansion_gateway.py ozon_expansion.py ozon_performance.py wb_expansion.py supply_plan.py seller_reports.py ./
COPY seller_views.py seller_ozon_views.py seller_wb_views.py seller_workbooks.py seller_insights.py report_jobs.py ./
COPY market_archive.py market_composites.py market_diagnostics.py market_health.py market_history.py posting_discovery.py supply_history.py wb_reports.py wb_legacy_reports.py ./
COPY calculator_ui.py cost_catalog.py cost_integration.py cost_sync.py ./
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--config", "gunicorn.conf.py", "--bind", "0.0.0.0:8000", "portal:create_portal()"]
