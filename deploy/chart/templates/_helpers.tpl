{{- define "market-intel.apiDns" -}}
{{- printf "%s.%s.svc.cluster.local" .name .root.Release.Namespace -}}
{{- end -}}

{{- define "market-intel.productionTlsPhase" -}}
{{- $tls := .Values.internalTls | default dict -}}
{{- $phase := $tls.productionPhase | default "" -}}
{{- if $phase -}}
{{- if or (not (has $phase (list "prepare" "switch" "retire"))) $tls.enabled
          (ne .Values.environment "production") (ne .Release.Namespace "market-intel")
          (not .Values.rollouts.enabled) (not .Values.rollouts.routeDashboardThroughAlb) -}}
{{- fail "internalTls.productionPhase must be prepare, switch or retire in the production weighted ALB release" -}}
{{- end -}}
{{- $phase -}}
{{- end -}}
{{- end -}}

{{- define "market-intel.newsMtlsMode" -}}
{{- $tls := .Values.internalTls | default dict -}}
{{- $mode := $tls.newsClientAuth | default "" -}}
{{- if $mode -}}
{{- $staging := and $tls.enabled (eq .Values.environment "staging") (eq .Release.Namespace "market-intel-staging") -}}
{{- $retired := eq (include "market-intel.productionTlsPhase" .) "retire" -}}
{{- if or (not (has $mode (list "prepare" "require"))) (not (or $staging $retired)) -}}
{{- fail "internalTls.newsClientAuth must be prepare or require on the TLS staging or retired production profile" -}}
{{- end -}}
{{- $mode -}}
{{- end -}}
{{- end -}}

{{- define "market-intel.newsMtlsProbe" -}}
exec:
  command:
    - python
    - /var/run/market-intel/news-mtls-probe/probe.py
    - {{ include "market-intel.apiDns" (dict "name" "news-api" "root" .root) | quote }}
    - {{ .port | quote }}
timeoutSeconds: 5
{{- end -}}

{{- define "market-intel.internalApiPorts" -}}
{{- $phase := include "market-intel.productionTlsPhase" .root -}}
{{- $port := (index .root.Values.services .name).port -}}
{{- if eq $phase "retire" -}}
{{- toJson (list 8443) -}}
{{- else if $phase -}}
{{- toJson (list $port 8443) -}}
{{- else -}}
{{- toJson (list $port) -}}
{{- end -}}
{{- end -}}

{{- define "market-intel.internalAlbPorts" -}}
{{- $phase := include "market-intel.productionTlsPhase" . -}}
{{- if eq $phase "retire" -}}
[443]
{{- else if eq $phase "switch" -}}
[80,443]
{{- else -}}
[80]
{{- end -}}
{{- end -}}

{{- define "market-intel.internalApiUrl" -}}
{{- $api := index .root.Values.services .name -}}
{{- $phase := include "market-intel.productionTlsPhase" .root -}}
{{- printf "https://%s:%v" (include "market-intel.apiDns" .) (ternary 8443 $api.port (ne $phase "")) -}}
{{- end -}}

{{- define "market-intel.servicePorts" -}}
{{- $phase := include "market-intel.productionTlsPhase" .root -}}
{{- $api := ne .name "dashboard" -}}
{{- if not (and $api (eq $phase "retire")) }}
- name: http
  port: {{ (index .root.Values.services .name).port }}
  targetPort: http
{{- end }}
{{- if and $api $phase }}
- name: https
  port: 8443
  targetPort: https
{{- end }}
{{- end -}}

{{/* Production website requests must traverse the same weighted ALB as analysis traffic. */}}
{{- define "market-intel.marketTrafficUrl" -}}
{{- $phase := include "market-intel.productionTlsPhase" . -}}
{{- if (default (dict) .Values.internalTls).enabled -}}
{{- if or (ne .Values.environment "staging") (ne .Release.Namespace "market-intel-staging") .Values.rollouts.enabled .Values.rollouts.routeDashboardThroughAlb -}}
{{- fail "internalTls.enabled is supported only for staging with direct API traffic" -}}
{{- end -}}
{{- include "market-intel.internalApiUrl" (dict "name" "market-api" "root" .) -}}
{{- else if .Values.rollouts.routeDashboardThroughAlb -}}
{{- if not .Values.rollouts.enabled -}}
{{- fail "rollouts.routeDashboardThroughAlb requires rollouts.enabled: true" -}}
{{- end -}}
{{- $hostname := required "rollouts.apiHostname is required for weighted dashboard traffic" .Values.rollouts.apiHostname -}}
{{- if not (regexMatch "^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$" $hostname) -}}
{{- fail "rollouts.apiHostname must be a DNS hostname without a scheme, path or port" -}}
{{- end -}}
{{- printf "%s://%s" (ternary "https" "http" (or (eq $phase "switch") (eq $phase "retire"))) $hostname -}}
{{- else -}}
{{- (index .Values.services "dashboard").env.MARKET_API_URL -}}
{{- end -}}
{{- end -}}

{{- define "market-intel.productionTlsCommand" -}}
command: [python, -m, uvicorn]
args:
  - {{ ternary "market.market_api:app" "market.news_api:app" (eq . "market-api") }}
  - --host
  - 0.0.0.0
  - --port
  - "8443"
  - --no-access-log
{{- end -}}

{{/* Deployments and Rollouts share the same pod security and resource settings. */}}
{{- define "market-intel.podSpec" -}}
{{- $service := .service -}}
{{- $image := required (printf "services.%s.image is required" .name) $service.image -}}
{{- $stagingTls := (default (dict) .root.Values.internalTls).enabled -}}
{{- $phase := include "market-intel.productionTlsPhase" .root -}}
{{- $retire := eq $phase "retire" -}}
{{- $switch := or (eq $phase "switch") $retire -}}
{{- $dashboard := eq .name "dashboard" -}}
{{- $apiTls := and (not $dashboard) (or $stagingTls $retire) -}}
{{- $dual := and (not $dashboard) (or (eq $phase "prepare") (eq $phase "switch")) -}}
{{- $dashboardTrust := and $dashboard (or $stagingTls $phase) -}}
{{- $newsMtls := include "market-intel.newsMtlsMode" .root -}}
{{- $newsClient := and $dashboard $newsMtls -}}
{{- $newsRequire := and (eq .name "news-api") (eq $newsMtls "require") -}}
{{- $primaryPort := ternary 8443 $service.port (and (not $dashboard) $retire) -}}
{{- $primaryPortName := ternary "https" "http" (and (not $dashboard) $retire) -}}
{{- if and .root.Values.requireImageDigest (not (regexMatch "^.+@sha256:[a-f0-9]{64}$" $image)) -}}
{{- fail (printf "services.%s.image must use an immutable sha256 digest" .name) -}}
{{- end -}}
automountServiceAccountToken: false
terminationGracePeriodSeconds: 60
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile:
    type: RuntimeDefault
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app: {{ .name }}
containers:
  - name: {{ .name }}
    image: {{ $image | quote }}
    imagePullPolicy: IfNotPresent
    {{- if and $apiTls $retire }}
    {{- include "market-intel.productionTlsCommand" .name | nindent 4 }}
    {{- end }}
    ports:
      - name: {{ $primaryPortName }}
        containerPort: {{ $primaryPort }}
    env:
      - name: PYTHONDONTWRITEBYTECODE
        value: '1'
      - name: HOME
        value: /tmp
      - name: XDG_CACHE_HOME
        value: /tmp/cache
      {{- if $apiTls }}
      - name: UVICORN_SSL_CERTFILE
        value: /var/run/market-intel/tls/tls.crt
      - name: UVICORN_SSL_KEYFILE
        value: /var/run/market-intel/tls/tls.key
      {{- else if $dashboardTrust }}
      - name: REQUESTS_CA_BUNDLE
        value: /var/run/market-intel/ca/ca.crt
      {{- end }}
      {{- if $newsClient }}
      - name: NEWS_API_CLIENT_CERT_FILE
        value: /var/run/market-intel/news-client/tls.crt
      - name: NEWS_API_CLIENT_KEY_FILE
        value: /var/run/market-intel/news-client/tls.key
      {{- end }}
      {{- if $newsRequire }}
      - name: UVICORN_SSL_CA_CERTS
        value: /var/run/market-intel/news-client-ca/ca.crt
      - name: UVICORN_SSL_CERT_REQS
        value: "2"
      {{- end }}
      {{- range $key, $value := $service.env }}
      - name: {{ $key }}
        {{- if and (eq $.name "dashboard") (eq $key "MARKET_API_URL") }}
        value: {{ include "market-intel.marketTrafficUrl" $.root | quote }}
        {{- else if and (or $stagingTls $switch) $dashboard (eq $key "NEWS_API_URL") }}
        value: {{ include "market-intel.internalApiUrl" (dict "name" "news-api" "root" $.root) | quote }}
        {{- else }}
        value: {{ $value | quote }}
        {{- end }}
      {{- end }}
    resources:
      requests:
        cpu: {{ default "100m" $service.cpuRequest }}
        memory: {{ default "128Mi" $service.memoryRequest }}
      limits:
        cpu: '1'
        memory: {{ $service.memory }}
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop: [ALL]
    volumeMounts:
      - name: tmp
        mountPath: /tmp
      {{- if $apiTls }}
      - name: internal-tls
        mountPath: /var/run/market-intel/tls
        readOnly: true
      {{- end }}
      {{- if or $dashboardTrust $newsRequire }}
      - name: internal-ca
        mountPath: /var/run/market-intel/ca
        readOnly: true
      {{- end }}
      {{- if $newsClient }}
      - name: news-client
        mountPath: /var/run/market-intel/news-client
        readOnly: true
      {{- end }}
      {{- if $newsRequire }}
      - name: news-client-ca
        mountPath: /var/run/market-intel/news-client-ca
        readOnly: true
      - name: news-probe-client
        mountPath: /var/run/market-intel/news-probe-client
        readOnly: true
      - name: news-mtls-probe
        mountPath: /var/run/market-intel/news-mtls-probe
        readOnly: true
      {{- end }}
    startupProbe:
      {{- if $newsRequire }}
      {{- include "market-intel.newsMtlsProbe" (dict "root" .root "port" $primaryPort) | nindent 6 }}
      {{- else }}
      httpGet:
        path: {{ $service.healthPath }}
        port: {{ $primaryPortName }}
        {{- if $apiTls }}
        scheme: HTTPS
        {{- end }}
      {{- end }}
      failureThreshold: 30
      periodSeconds: 5
    readinessProbe:
      {{- if $newsRequire }}
      {{- include "market-intel.newsMtlsProbe" (dict "root" .root "port" $primaryPort) | nindent 6 }}
      {{- else }}
      httpGet:
        path: {{ $service.healthPath }}
        port: {{ $primaryPortName }}
        {{- if $apiTls }}
        scheme: HTTPS
        {{- end }}
      {{- end }}
      periodSeconds: 10
    livenessProbe:
      {{- if $newsRequire }}
      {{- include "market-intel.newsMtlsProbe" (dict "root" .root "port" $primaryPort) | nindent 6 }}
      {{- else }}
      httpGet:
        path: {{ $service.healthPath }}
        port: {{ $primaryPortName }}
        {{- if $apiTls }}
        scheme: HTTPS
        {{- end }}
      {{- end }}
      periodSeconds: 20
  {{- if $dual }}
  - name: {{ .name }}-tls
    image: {{ $image | quote }}
    imagePullPolicy: IfNotPresent
    {{- include "market-intel.productionTlsCommand" .name | nindent 4 }}
    ports:
      - name: https
        containerPort: 8443
    env:
      - name: PYTHONDONTWRITEBYTECODE
        value: '1'
      - name: HOME
        value: /tmp
      - name: XDG_CACHE_HOME
        value: /tmp/cache
      - name: UVICORN_SSL_CERTFILE
        value: /var/run/market-intel/tls/tls.crt
      - name: UVICORN_SSL_KEYFILE
        value: /var/run/market-intel/tls/tls.key
      {{- range $key, $value := $service.env }}
      - name: {{ $key }}
        value: {{ $value | quote }}
      {{- end }}
    resources:
      requests:
        cpu: {{ default "100m" $service.cpuRequest }}
        memory: {{ default "128Mi" $service.memoryRequest }}
      limits:
        cpu: '1'
        memory: {{ $service.memory }}
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop: [ALL]
    volumeMounts:
      - name: tls-tmp
        mountPath: /tmp
      - name: internal-tls
        mountPath: /var/run/market-intel/tls
        readOnly: true
    startupProbe:
      httpGet: {path: {{ $service.healthPath }}, port: https, scheme: HTTPS}
      failureThreshold: 30
      periodSeconds: 5
    readinessProbe:
      httpGet: {path: {{ $service.healthPath }}, port: https, scheme: HTTPS}
      periodSeconds: 10
    livenessProbe:
      httpGet: {path: {{ $service.healthPath }}, port: https, scheme: HTTPS}
      periodSeconds: 20
  {{- end }}
volumes:
  - name: tmp
    emptyDir:
      sizeLimit: 256Mi
  {{- if $dual }}
  - name: tls-tmp
    emptyDir:
      sizeLimit: 256Mi
  {{- end }}
  {{- if or $apiTls $dual }}
  - name: internal-tls
    secret:
      secretName: {{ printf "%s-tls" .name | quote }}
      defaultMode: 288
  {{- end }}
  {{- if or $dashboardTrust $newsRequire }}
  - name: internal-ca
    configMap:
      name: market-intel-ca
      items:
        - key: ca.crt
          path: ca.crt
  {{- end }}
  {{- if $newsClient }}
  - name: news-client
    secret:
      secretName: dashboard-news-client-tls
      defaultMode: 288
  {{- end }}
  {{- if $newsRequire }}
  - name: news-client-ca
    configMap:
      name: market-intel-news-client-ca
      items:
        - key: ca.crt
          path: ca.crt
  - name: news-probe-client
    secret:
      secretName: news-probe-client-tls
      defaultMode: 288
  - name: news-mtls-probe
    configMap:
      name: market-intel-news-mtls-probe
  {{- end }}
{{- end -}}
