#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/market-intel-bootstrap.log) 2>&1
umask 077
dnf upgrade -y
dnf install -y docker git openssl python3
systemctl enable --now docker
install -d /usr/local/lib/docker/cli-plugins /opt/market-intel-secrets
cd /tmp
curl --fail --location --retry 3 -o docker-compose https://github.com/docker/compose/releases/download/v5.6.1/docker-compose-linux-x86_64
curl --fail --location --retry 3 -o compose.sha256 https://github.com/docker/compose/releases/download/v5.6.1/docker-compose-linux-x86_64.sha256
printf '%s  docker-compose\n' "$(cut -d' ' -f1 compose.sha256)" | sha256sum -c -
install -m 755 docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
curl --fail --location --retry 3 -o buildx-v0.38.1.linux-amd64 https://github.com/docker/buildx/releases/download/v0.38.1/buildx-v0.38.1.linux-amd64
curl --fail --location --retry 3 -o buildx-checksums.txt https://github.com/docker/buildx/releases/download/v0.38.1/checksums.txt
grep 'buildx-v0.38.1.linux-amd64$' buildx-checksums.txt | sha256sum -c -
install -m 755 buildx-v0.38.1.linux-amd64 /usr/local/lib/docker/cli-plugins/docker-buildx
if ! test -d /opt/market-intel/.git; then git clone https://github.com/Saisharath123/market_intel.git /opt/market-intel; fi
cd /opt/market-intel
git fetch origin "$PROJECT_REVISION"
git checkout --detach "$PROJECT_REVISION"
# The checked-out public source and monitoring files must be readable by non-root containers.
chmod -R a+rX /opt/market-intel
if ! test -s /opt/market-intel-secrets/grafana-admin-password; then openssl rand -hex 24 > /opt/market-intel-secrets/grafana-admin-password; fi
chmod 755 /opt/market-intel-secrets
chmod 444 /opt/market-intel-secrets/grafana-admin-password
# Password is readable by the Grafana container; its host parent is accessible only to root.
chmod 700 /opt/market-intel-secrets
mkdir -p /etc/docker
python3 - <<'PY'
import json
from pathlib import Path
p=Path('/etc/docker/daemon.json'); d=json.loads(p.read_text()) if p.exists() else {}
d.update({'log-driver':'json-file','log-opts':{'max-size':'10m','max-file':'3'}})
p.write_text(json.dumps(d))
PY
systemctl restart docker
for service in market-api news-api dashboard; do docker compose -f compose.yaml -f deploy/ec2/compose.yaml build --pull "$service"; done
docker compose -f compose.yaml -f deploy/ec2/compose.yaml pull prometheus grafana node-exporter blackbox
cat > /etc/systemd/system/market-intel.service <<'UNIT'
[Unit]
Description=Market Intel application and observability
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/market-intel
ExecStart=/usr/bin/docker compose -f compose.yaml -f deploy/ec2/compose.yaml up -d
ExecStop=/usr/bin/docker compose -f compose.yaml -f deploy/ec2/compose.yaml stop
TimeoutStartSec=300
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now market-intel
printf 'MARKET_INTEL_BOOTSTRAP_COMPLETE\n'
