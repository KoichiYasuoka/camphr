FROM docker.pkg.github.com/tamuhey/dockerfiles/mecab:ci-build
COPY . /app
WORKDIR /app
RUN pip install -U poetry && poetry install -E mecab
