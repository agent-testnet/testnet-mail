FROM golang:1.22-alpine AS build
WORKDIR /src
COPY main.go .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /signup-api main.go

FROM alpine:3.20
RUN adduser -D -u 10001 appuser
COPY --from=build /signup-api /usr/local/bin/signup-api
USER appuser
EXPOSE 8081
ENTRYPOINT ["signup-api"]
