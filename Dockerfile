FROM python:3.13-alpine

# tzdata ships with this image; fail the build if the zone database is missing
RUN python -c "import zoneinfo; zoneinfo.ZoneInfo('Europe/Paris')" \
 && adduser -D -H -u 10001 redeployer \
 && mkdir -p /data && chown redeployer:redeployer /data

WORKDIR /app
COPY redeployer/ /app/redeployer/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    HTTP_PORT=8080

USER redeployer
VOLUME ["/data"]
EXPOSE 8080
ENTRYPOINT ["python", "-m", "redeployer"]
CMD ["serve"]
