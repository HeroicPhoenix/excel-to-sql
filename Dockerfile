FROM python:3.10-slim

WORKDIR /app
COPY . /app

# 升级 pip/工具链，并把默认超时调高
RUN pip install --no-cache-dir  -r requirements.txt

EXPOSE 12080
CMD ["python", "app.py"]