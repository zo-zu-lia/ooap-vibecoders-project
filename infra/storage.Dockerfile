# Upstream retired prebuilt community images. Build the fixed official source.
# One compilation at a time bounds memory consumption on a development machine.
FROM golang:1.25-alpine@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS build
RUN apk add --no-cache git
WORKDIR /src
RUN git init && git remote add origin https://github.com/minio/minio.git \
    && git fetch --depth 1 origin 9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a \
    && git checkout FETCH_HEAD
ENV GOMAXPROCS=2 GOMEMLIMIT=2GiB CGO_ENABLED=0
RUN go build -p 1 -trimpath -ldflags="-s -w" -o /minio .

FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache ca-certificates && adduser -D -u 10001 minio \
    && mkdir /data && chown minio /data
COPY --from=build /minio /usr/local/bin/minio
COPY --from=build /src/LICENSE /usr/share/licenses/minio/LICENSE
USER minio
EXPOSE 9000
ENTRYPOINT ["minio"]
