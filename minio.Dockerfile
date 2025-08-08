# minio.Dockerfile
FROM minio/minio:latest

# Render will inject these from render.yaml
ENV MINIO_ROOT_USER=changeme \
    MINIO_ROOT_PASSWORD=changeme \
    PORT=10000

# MinIO data lives here; Render will mount a disk at /data
VOLUME /data

EXPOSE 10000

# Run MinIO server on Render's single port
ENTRYPOINT ["/usr/bin/minio"]
CMD ["server", "/data", "--address", ":10000"]
