# The isolated execution zone (design doc §2).
#
# This is the only image in the system built to hold a Docker socket -- see
# docker-compose.yml, where it is the only service with a
# `/var/run/docker.sock` volume mount. It does not COPY `app/` at all: it has
# no reason to import anything from the trusted zone, and shouldn't be able
# to even if a bug tried.

FROM python:3.12-slim

WORKDIR /srv

COPY docker/execution-service.requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY execution_service ./execution_service

ENV CITADEL_EXECUTION_HOST=0.0.0.0 \
    CITADEL_EXECUTION_PORT=8901

EXPOSE 8901

CMD ["python", "-m", "execution_service"]
