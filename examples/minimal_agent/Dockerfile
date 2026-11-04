FROM python:3.12-slim
WORKDIR /agent
COPY agent.py .
RUN useradd --system --uid 10001 agent
USER agent
EXPOSE 8080
CMD ["python", "agent.py"]
