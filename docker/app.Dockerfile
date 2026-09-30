# The trusted workflow zone (design doc §2): CLI-facing API, Query Router,
# Orchestrator, Control Plane, Model Router, Data Plane -- one FastAPI
# process, consolidated for build speed (§2's explicit MVP simplification).
#
# Does NOT install the `docker` package and does NOT mount a Docker socket
# (see docker-compose.yml) -- this image must never be able to reach one.

FROM python:3.12-slim

WORKDIR /srv

COPY docker/app.requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV CITADEL_EXECUTION_SERVICE_URL=http://execution-service:8901

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
