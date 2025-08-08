FROM python:3.10-slim

WORKDIR /app
COPY . /app

# 可选：国内常见镜像（取消下面两行注释即可）
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

# 升级 pip/工具链，并把默认超时调高
RUN pip install --no-cache-dir  -r requirements.txt

EXPOSE 12080
CMD ["python", "app.py"]