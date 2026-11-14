#!/bin/sh
set -eu
TLS_DIR=/var/lib/postgresql/tls
CA_DIR=/var/lib/postgresql/ca
mkdir -p "$TLS_DIR" "$CA_DIR"
if [ ! -s "$TLS_DIR/server.key" ] || [ ! -s "$TLS_DIR/server.crt" ]; then
  umask 077
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 \
    -subj /CN=aidocumines-postgresql \
    -addext "subjectAltName=DNS:database,DNS:aidocumines_api_layer_database,DNS:aidocumines_api_layer_db,DNS:localhost,IP:127.0.0.1" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" 2>/dev/null
fi
# Only the public certificate is shared with application containers.
cp "$TLS_DIR/server.crt" "$CA_DIR/server.crt"
chown -R postgres:postgres "$TLS_DIR" "$CA_DIR"
chmod 700 "$TLS_DIR"
chmod 600 "$TLS_DIR/server.key"
chmod 755 "$CA_DIR"
chmod 644 "$CA_DIR/server.crt"
exec /usr/local/bin/docker-entrypoint.sh "$@"
