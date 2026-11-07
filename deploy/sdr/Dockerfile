FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=America/Sao_Paulo
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libldap2-dev libsasl2-dev libpq-dev libmagic1 libxml2-dev libxslt1-dev \
    fontconfig fonts-dejavu-core wkhtmltopdf node-less npm ca-certificates curl && \
    npm install -g rtlcss && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/odoo-requirements.txt
RUN pip install --no-cache-dir -r /tmp/odoo-requirements.txt && \
    useradd --system --uid 10001 --create-home odoo && install -d -o odoo -g odoo /var/lib/odoo
USER odoo
WORKDIR /opt/odoo
ENTRYPOINT ["python", "/opt/odoo/odoo-bin"]
