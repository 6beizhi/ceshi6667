FROM python:3.12-slim
WORKDIR /app
COPY app.py ./
COPY static ./static
RUN mkdir -p /data && useradd -r -u 10001 petsafe && chown -R petsafe:petsafe /data /app
USER petsafe
ENV HOST=0.0.0.0 PORT=8000 PETSAFE_DATA_DIR=/data
EXPOSE 8000
CMD ["python", "app.py"]
