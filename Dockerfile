FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY paper_service.py paper_live.py config.py chain.py replay.py executor.py ./
COPY data/wallets.json data/wallets.json
EXPOSE 10000
CMD ["python", "paper_service.py"]
