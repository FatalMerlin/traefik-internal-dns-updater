FROM python:3-alpine
# `bind-tools` is needed for `nsupdate` => https://pkgs.alpinelinux.org/contents?branch=edge&name=bind-tools&arch=x86_64&repo=main
RUN apk update && apk add bind-tools

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py .

CMD ["python", "main.py"]