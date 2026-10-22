FROM postgres:17.11-alpine

RUN apk add --no-cache python3 py3-pip \
    && python3 -m venv /opt/backup-venv \
    && /opt/backup-venv/bin/pip install --no-cache-dir boto3==1.43.116
COPY deploy/backup/backup.py /opt/backup.py
USER 70:70
ENTRYPOINT ["/opt/backup-venv/bin/python", "/opt/backup.py"]
